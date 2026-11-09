// Streaming ROOT analysis; numerical definitions are shared with the live monitor.
#include "../data-ana-monitor/online/adaptive_waveform.h"
#include <TFile.h>
#include <TTree.h>
#include <TTreeReader.h>
#include <TTreeReaderValue.h>
#include <TH1D.h>
#include <TH2D.h>
#include <TProfile.h>
#include <TGraph.h>
#include <TNamed.h>
#include <array>
#include <cstdlib>
#ifdef __linux__
#include <sys/file.h>
#include <fcntl.h>
#include <unistd.h>
#include <sched.h>
#endif
#include <chrono>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <memory>
#include <set>
#include <stdexcept>

struct Metric { const char *name, *unit; double low, high; int bins; };
static const Metric metrics[] = {
    {"baseline","ADC",100,400,600}, {"noise","ADC",0,20,400},
    {"amplitude","ADC",0,4096,512}, {"area","ADC.sample",-500,25000,510},
    {"charge_pc","pC (historical scale)",-10,500,510},
    {"tail_negative","ADC.sample",0,25000,500},
    {"peak_ns","ns",0,1024,512}, {"cfd_ns","ns",0,1024,512},
    {"threshold_ns","ns",0,1024,512}, {"fwhm_ns","ns",0,200,400},
    {"rise_ns","ns",0,200,400}, {"fall_ns","ns",0,400,400},
    {"wave_dt_ns","ns",-1024,2048,512}, {"pulses","pulses",-.5,16.5,17},
    {"window_ns","ns",0,1024,512}, {"snr","amplitude/noise",0,512,512}
};
constexpr int NM=sizeof(metrics)/sizeof(metrics[0]);
struct Moments {
    long long n=0, missing=0; double mean=0, m2=0, lo=INFINITY, hi=-INFINITY;
    void add(double v) {
        if(!std::isfinite(v)) {++missing; return;}
        ++n; double d=v-mean; mean+=d/n; m2+=d*(v-mean);
        lo=std::min(lo,v); hi=std::max(hi,v);
    }
};
struct TrendBin {
    long long n=0, good=0, cfd_n=0;
    double baseline=0, noise=0, area=0, amplitude=0, cfd=0;
};
struct Channel {
    long long n=0, good=0, density_n=0, zero_dt=0, negative_dt=0, last_ns=-1;
    std::array<std::array<TH1D*,NM>,2> hist{};
    std::array<std::array<Moments,NM>,2> stats{};
    TH1D *flags=nullptr, *interval=nullptr;
    TH2D *density=nullptr;
    std::map<std::pair<int,int>,TrendBin> trends;
    // Arrays are keyed by waveform length, so a shape change cannot mix averages.
    std::map<int,std::vector<double>> sums;
    std::map<int,long long> shape_counts;
    std::vector<double> aligned_sum=std::vector<double>(512), aligned_n=std::vector<double>(512);
    std::vector<std::vector<unsigned short>> examples;
    std::vector<seastar::Pulse> example_pulses;
};

int main(int argc,char **argv) {
    try {
        if(argc!=8) throw std::runtime_error("usage: analyzer input.root output.root tables-dir config.txt board max-events summary.json");
        const std::string input=argv[1], output=argv[2], tables=argv[3];
        int expected_board=std::stoi(argv[5]); long long limit=std::stoll(argv[6]);
        seastar::Config cfg; double adc_mv, resistance; int stride;
        std::ifstream config(argv[4]);
        config>>cfg.sample_ns>>cfg.snr>>cfg.end_sigma>>cfg.end_fraction>>cfg.cfd_fraction
              >>cfg.threshold_adc>>cfg.timing_snr>>cfg.adc_max>>cfg.polarity>>cfg.min_baseline
              >>cfg.return_run>>cfg.padding>>adc_mv>>resistance>>stride;
        if(!config) throw std::runtime_error("invalid analysis config");
        TFile source(input.c_str(),"READ");
        if(source.IsZombie() || source.TestBit(TFile::kRecovered)) throw std::runtime_error("invalid input ROOT file");
        auto tree=dynamic_cast<TTree*>(source.Get("seastar"));
        if(!tree || tree->GetEntries()==0) throw std::runtime_error("missing or empty seastar tree");
        long long total=tree->GetEntries(), wanted=limit>0?std::min(total,limit):total;
        TTreeReader reader(tree);
        TTreeReaderValue<Int_t> nw(reader,"n_waves");
        TTreeReaderValue<UShort_t> tn(reader,"trigger_number");
        TTreeReaderValue<UInt_t> ts(reader,"trigger_second"), tns(reader,"trigger_nanosecond");
        TTreeReaderValue<std::vector<unsigned char>> boards(reader,"hunt_id"), channels(reader,"ch_id"), ids(reader,"wave_id");
        TTreeReaderValue<std::vector<unsigned int>> seconds(reader,"second"), nanos(reader,"nanosecond");
        TTreeReaderValue<std::vector<std::vector<unsigned short>>> samples(reader,"samples");
        TFile dest(output.c_str(),"CREATE","SeaStar batch waveform analysis",101);
        if(dest.IsZombie()) throw std::runtime_error("cannot create output ROOT file");
        TH1::AddDirectory(false);
        TTree rows("metrics","All waveform metrics; NaN means missing timing; no raw samples copied");
        rows.SetAutoFlush(-32*1024*1024);
        Long64_t event=0, raw_ns=0, trigger_ns=0; int board=0,ch=0,wave_id=0,segment=0,points=0,good=0;
        unsigned short trigger_number=0; double t=0,charge_pc=0, amplitude_mv=0, wave_dt_ns=0, window_ns=0,snr=0;
        seastar::Pulse pulse;
#define BR(name,var) rows.Branch(name,&var)
        BR("event",event); BR("board",board); BR("channel",ch); BR("wave_id",wave_id);
        BR("raw_ns",raw_ns); BR("trigger_ns",trigger_ns); BR("trigger_number",trigger_number);
        BR("segment",segment); BR("t",t); BR("points",points); BR("good",good);
        BR("charge_pc",charge_pc); BR("amplitude_mv",amplitude_mv); BR("wave_dt_ns",wave_dt_ns);
        BR("window_ns",window_ns); BR("snr",snr);
#define PBR(name) BR(#name,pulse.name)
        PBR(baseline); PBR(noise); PBR(amplitude); PBR(area); PBR(tail_negative);
        PBR(peak_ns); PBR(cfd_ns); PBR(threshold_ns); PBR(fwhm_ns); PBR(rise_ns); PBR(fall_ns);
        PBR(start); PBR(end); PBR(peak); PBR(polarity); PBR(pulses); PBR(baseline_points); PBR(flags);
#undef PBR
#undef BR
        std::map<int,Channel> data;
        auto init=[&](int c)->Channel& {
            auto inserted=data.try_emplace(c);
            auto &d=inserted.first->second;
            if(!inserted.second) return d;
            for(int pop=0;pop<2;++pop) for(int m=0;m<NM;++m) {
                std::string name=std::string(pop?"good_":"all_")+metrics[m].name+"_ch"+std::to_string(c);
                d.hist[pop][m]=new TH1D(name.c_str(),(";"+std::string(metrics[m].unit)+";Waveforms").c_str(),metrics[m].bins,metrics[m].low,metrics[m].high);
                if(m!=13) d.hist[pop][m]->SetCanExtend(TH1::kXaxis);
            }
            d.flags=new TH1D(("flags_ch"+std::to_string(c)).c_str(),";Flag bit;Waveforms",11,.5,11.5);
            d.interval=new TH1D(("interval_ch"+std::to_string(c)).c_str(),";log10(interval / s);Intervals",240,-9,3);
            d.density=new TH2D(("density_ch"+std::to_string(c)).c_str(),";Fractional waveform position;Raw ADC",128,0,1,128,0,cfg.adc_max+1.);
            return d;
        };
        TH2D coincidence("coincidence",";Channel;Channel",8,-.5,7.5,8,-.5,7.5);
        TH1D multiplicity("multiplicity",";Waveforms per saved event;Events",65,-.5,64.5);
        multiplicity.SetCanExtend(TH1::kXaxis);
        TH1D trigger_interval("trigger_interval",";log10(interval / s);Intervals",240,-9,3);
        std::map<std::pair<int,int>,TH1D*> pairs;
        std::map<std::pair<int,int>,long long> event_bins;
        std::map<int,double> segment_end;
        long long first_ns=-1, previous_ns=-1, waves=0, qualified=0, events=0, empty=0;
        auto start=std::chrono::steady_clock::now();
        std::ofstream trend_file(tables+"/trends.csv");
        // On a shared rotational disk, take turns in bounded batches; other strings stay active.
#ifdef __linux__
        const char* gate_path=std::getenv("SEASTAR_READ_GATE");
        int gate=gate_path?open(gate_path,O_CREAT|O_RDWR,0600):-1;
        if(gate>=0) flock(gate,LOCK_EX);
#endif
        while(events<wanted && reader.Next()) {
#ifdef __linux__
            if(gate>=0 && events && events%8192==0) {flock(gate,LOCK_UN);sched_yield();flock(gate,LOCK_EX);}
#endif
            event=events++; trigger_number=*tn;
            if(*tns>=1000000000) throw std::runtime_error("invalid trigger nanoseconds");
            trigger_ns=static_cast<long long>(*ts)*1000000000+*tns;
            if(samples->size()!=channels->size() || samples->size()!=boards->size() || samples->size()!=ids->size() ||
               samples->size()!=seconds->size() || samples->size()!=nanos->size() || int(samples->size())!=*nw)
                throw std::runtime_error("inconsistent input waveform arrays");
            multiplicity.Fill(*nw);
            if(*nw==0) {++empty; continue;}
            if(first_ns<0) first_ns=trigger_ns;
            if(previous_ns>=0 && trigger_ns<previous_ns) {
                ++segment; first_ns=trigger_ns;
                for(auto &item:data) item.second.last_ns=-1;
            } else if(previous_ns>=0 && trigger_ns>previous_ns)
                trigger_interval.Fill(std::log10((trigger_ns-previous_ns)*1e-9));
            previous_ns=trigger_ns; t=(trigger_ns-first_ns)*1e-9;
            segment_end[segment]=t; int bin=int(t/100.);
            ++event_bins[{segment,bin}];
            std::map<int,double> event_channels;
            for(size_t i=0;i<samples->size();++i) {
                board=(*boards)[i]; ch=(*channels)[i]; wave_id=(*ids)[i]; const auto &wave=(*samples)[i];
                if(board!=expected_board || ch<0 || ch>=8 || wave.empty() || wave.size()>8192 || (*nanos)[i]>=1000000000)
                    throw std::runtime_error("unexpected board/channel/waveform shape");
                points=wave.size(); raw_ns=static_cast<long long>((*seconds)[i])*1000000000+(*nanos)[i];
                pulse=seastar::analyze(wave.data(),points,cfg); good=seastar::good(pulse);
                charge_pc=pulse.area*adc_mv*cfg.sample_ns/resistance; amplitude_mv=pulse.amplitude*adc_mv;
                wave_dt_ns=raw_ns-trigger_ns; window_ns=(pulse.end-pulse.start)*cfg.sample_ns;
                snr=pulse.noise?pulse.amplitude/pulse.noise:0;
                double values[NM]={pulse.baseline,pulse.noise,pulse.amplitude,pulse.area,charge_pc,pulse.tail_negative,
                    pulse.peak_ns,pulse.cfd_ns,pulse.threshold_ns,pulse.fwhm_ns,pulse.rise_ns,pulse.fall_ns,
                    wave_dt_ns,double(pulse.pulses),window_ns,snr};
                auto &d=init(ch); ++d.n; ++waves; d.good+=good; qualified+=good;
                for(int pop=0;pop<=good;++pop) for(int m=0;m<NM;++m) {
                    d.stats[pop][m].add(values[m]); if(std::isfinite(values[m])) d.hist[pop][m]->Fill(values[m]);
                }
                for(int bit=0;bit<11;++bit) if(pulse.flags&(1u<<bit)) d.flags->Fill(bit+1);
                if(d.last_ns>=0) {
                    auto dt=raw_ns-d.last_ns;
                    if(dt>0) d.interval->Fill(std::log10(dt*1e-9)); else if(dt==0) ++d.zero_dt; else ++d.negative_dt;
                }
                d.last_ns=raw_ns;
                auto &b=d.trends[{segment,bin}]; ++b.n; b.good+=good;
                b.baseline+=pulse.baseline; b.noise+=pulse.noise;
                if(good) {
                    b.area+=pulse.area; b.amplitude+=pulse.amplitude;
                    if(std::isfinite(pulse.cfd_ns)) {b.cfd+=pulse.cfd_ns; ++b.cfd_n;}
                    auto &sum=d.sums[points]; if(sum.empty()) sum.resize(points);
                    ++d.shape_counts[points];
                    for(int j=0;j<points;++j) sum[j]+=wave[j];
                    for(int j=std::max(0,pulse.peak-128);j<std::min(points,pulse.peak+384);++j) {
                        int k=j-pulse.peak+128; d.aligned_sum[k]+=pulse.polarity*(wave[j]-pulse.baseline); ++d.aligned_n[k];
                    }
                    if(d.good%stride==0) {
                        ++d.density_n;
                        for(int j=0;j<points;++j) d.density->Fill(double(j)/points,wave[j]);
                    }
                }
                if(d.examples.size()<8 && (d.examples.empty() || d.n%stride==1)) {
                    d.examples.push_back(wave); d.example_pulses.push_back(pulse);
                }
                event_channels.emplace(ch,good?pulse.cfd_ns:NAN);
                if(rows.Fill()<0) throw std::runtime_error("metric ROOT write failed");
            }
            for(auto a=event_channels.begin();a!=event_channels.end();++a) {
                coincidence.Fill(a->first,a->first);
                for(auto b=std::next(a);b!=event_channels.end();++b) {
                    coincidence.Fill(a->first,b->first); coincidence.Fill(b->first,a->first);
                    if(!std::isfinite(a->second) || !std::isfinite(b->second)) continue;
                    auto key=std::make_pair(a->first,b->first); auto &h=pairs[key];
                    if(!h) {h=new TH1D(("pair_"+std::to_string(key.first)+"_"+std::to_string(key.second)).c_str(),";CFD difference [ns];Events",400,-100,100); h->SetCanExtend(TH1::kXaxis);}
                    h->Fill(b->second-a->second);
                }
            }
            if(events%250000==0) {
                double elapsed=std::chrono::duration<double>(std::chrono::steady_clock::now()-start).count();
                std::cout<<"events="<<events<<"/"<<wanted<<" waves="<<waves<<" good="<<qualified<<" waves/s="<<waves/std::max(.001,elapsed)<<std::endl;
            }
        }
#ifdef __linux__
        if(gate>=0){flock(gate,LOCK_UN);close(gate);}
#endif
        if(events!=wanted) throw std::runtime_error("ROOT reader ended before expected event count");
        dest.cd(); rows.Write(); coincidence.Write(); multiplicity.Write(); trigger_interval.Write();
        for(auto &item:pairs) item.second->Write();
        std::ofstream summary(tables+"/channel_statistics.csv"); summary<<std::setprecision(17);
        summary<<"board,channel,population,metric,unit,n,missing,mean,sigma,min,max\n";
        trend_file<<std::setprecision(17)<<"board,channel,segment,t_start_s,t_end_s,exposure_s,waves,good,rate_hz,good_fraction,baseline_adc,noise_adc,area_adc_sample,amplitude_adc,cfd_ns\n";
        auto graph=[&](std::string name,const std::vector<double>&x,const std::vector<double>&y) {
            TGraph g(x.size(),x.data(),y.data()); g.SetName(name.c_str()); g.Write();
        };
        std::ofstream quality(tables+"/quality.csv"); quality<<"board,channel,waves,good,density_waveforms,zero_intervals,negative_intervals";
        for(int k=0;k<11;++k) quality<<",flag_"<<(1u<<k); quality<<"\n";
        for(auto &item:data) {
            auto c=item.first; auto &d=item.second;
            quality<<expected_board<<","<<c<<","<<d.n<<","<<d.good<<","<<d.density_n<<","<<d.zero_dt<<","<<d.negative_dt;
            for(int k=1;k<=11;++k) quality<<","<<static_cast<long long>(d.flags->GetBinContent(k)); quality<<"\n";
            for(int pop=0;pop<2;++pop) for(int m=0;m<NM;++m) {
                d.hist[pop][m]->Write(); auto &s=d.stats[pop][m];
                summary<<expected_board<<","<<c<<","<<(pop?"good":"all")<<","<<metrics[m].name<<","<<metrics[m].unit<<","<<s.n<<","<<s.missing;
                if(s.n) summary<<","<<s.mean<<","<<std::sqrt(std::max(0.,s.m2/s.n))<<","<<s.lo<<","<<s.hi;
                else summary<<",,,,"; summary<<"\n";
            }
            d.flags->Write(); d.interval->Write(); d.density->Write();
            for(auto &shape:d.sums) {
                std::vector<double>x(shape.first), y(shape.first);
                for(int j=0;j<shape.first;++j) {x[j]=j*cfg.sample_ns; y[j]=shape.second[j]/d.shape_counts[shape.first];}
                graph("average_ch"+std::to_string(c)+"_n"+std::to_string(shape.first),x,y);
            }
            std::vector<double>x,y;
            for(int j=0;j<512;++j) if(d.aligned_n[j]) {x.push_back((j-128)*cfg.sample_ns);y.push_back(d.aligned_sum[j]/d.aligned_n[j]);}
            graph("aligned_ch"+std::to_string(c),x,y);
            for(size_t k=0;k<d.examples.size();++k) {
                auto &wave=d.examples[k]; std::vector<double> ex(wave.size()),ey(wave.begin(),wave.end());
                for(size_t j=0;j<wave.size();++j) ex[j]=j*cfg.sample_ns;
                graph("example_ch"+std::to_string(c)+"_"+std::to_string(k),ex,ey);
                auto &p=d.example_pulses[k];
                TNamed(("example_window_ch"+std::to_string(c)+"_"+std::to_string(k)).c_str(),
                    (std::to_string(p.start*cfg.sample_ns)+","+std::to_string(p.end*cfg.sample_ns)+","+std::to_string(p.baseline)).c_str()).Write();
            }
            for(auto seg:segment_end) {
                std::vector<double> times, rate, good_fraction, baselines, noises, areas;
                int last=int(seg.second/100.);
                for(int k=0;k<=last;++k) {
                    double low=k*100., high=std::min(seg.second,(k+1)*100.), exposure=high-low;
                    auto &b=d.trends[{seg.first,k}];
                    if(exposure<=0) continue;
                    trend_file<<expected_board<<","<<c<<","<<seg.first<<","<<low<<","<<high<<","<<exposure<<","<<b.n<<","<<b.good<<","<<b.n/exposure<<",";
                    if(b.n) trend_file<<double(b.good)/b.n; trend_file<<",";
                    if(b.n) trend_file<<b.baseline/b.n; trend_file<<",";
                    if(b.n) trend_file<<b.noise/b.n; trend_file<<",";
                    if(b.good) trend_file<<b.area/b.good; trend_file<<",";
                    if(b.good) trend_file<<b.amplitude/b.good; trend_file<<",";
                    if(b.cfd_n) trend_file<<b.cfd/b.cfd_n; trend_file<<"\n";
                    times.push_back((low+high)/2); rate.push_back(b.n/exposure);
                    good_fraction.push_back(b.n?100.*b.good/b.n:NAN);
                    baselines.push_back(b.n?b.baseline/b.n:NAN); noises.push_back(b.n?b.noise/b.n:NAN);
                    areas.push_back(b.good?b.area/b.good:NAN);
                }
                std::string tag="_ch"+std::to_string(c)+"_segment"+std::to_string(seg.first);
                graph("rate"+tag,times,rate); graph("good_fraction"+tag,times,good_fraction);
                graph("baseline_trend"+tag,times,baselines); graph("noise_trend"+tag,times,noises); graph("area_trend"+tag,times,areas);
            }
        }
        std::ofstream trigger_trends(tables+"/trigger_rates.csv");
        trigger_trends<<"segment,t_start_s,t_end_s,exposure_s,wave_events,rate_hz\n";
        for(auto seg:segment_end) {
            std::vector<double>x,y;
            for(int k=0;k<=int(seg.second/100.);++k) {
                double low=k*100., high=std::min(seg.second,(k+1)*100.);
                if(high<=low) continue;
                auto n=event_bins[{seg.first,k}]; x.push_back((low+high)/2);y.push_back(n/(high-low));
                trigger_trends<<seg.first<<","<<low<<","<<high<<","<<high-low<<","<<n<<","<<n/(high-low)<<"\n";
            }
            graph("trigger_rate_segment"+std::to_string(seg.first),x,y);
        }
        rows.SetDirectory(nullptr); dest.Close();
        double elapsed=std::chrono::duration<double>(std::chrono::steady_clock::now()-start).count();
        std::ofstream result(argv[7]); result<<std::setprecision(17)<<"{\"input_entries\":"<<total<<",\"events\":"<<events<<",\"waves\":"<<waves
            <<",\"good\":"<<qualified<<",\"empty_saved_events\":"<<empty<<",\"segments\":"<<segment_end.size()<<",\"seconds\":"<<elapsed<<",\"segment_spans_s\":[";
        bool comma=false; for(auto seg:segment_end) {if(comma) result<<","; result<<seg.second;comma=true;}
        result<<"]}\n";
        std::cout<<"COMPLETE events="<<events<<" waves="<<waves<<" good="<<qualified<<" seconds="<<elapsed<<std::endl;
        return 0;
    } catch(const std::exception &e) {std::cerr<<"ERROR: "<<e.what()<<std::endl;return 1;}
}
