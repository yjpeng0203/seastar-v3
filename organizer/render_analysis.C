// Native ROOT report rendering from saved analysis objects and exact focus bins.
#include <TFile.h>
#include <TCanvas.h>
#include <TH1.h>
#include <TH2.h>
#include <THnSparse.h>
#include <memory>
#include <TGraph.h>
#include <TStyle.h>
#include <TLegend.h>
#include <TLatex.h>
#include <TLine.h>
#include <TBox.h>
#include <TColor.h>
#include <TKey.h>
#include <TSystem.h>
#include <TROOT.h>
#include <TNamed.h>
#include <algorithm>
#include <array>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <string>
#include <vector>
#include <map>
#include <set>
#include <cmath>
#include <stdexcept>

namespace seastar_report {
struct Metric {std::string key,title,unit;};
const Metric metrics[]={
    {"charge_pc","Main-pulse charge","pC (historical scale)"}, {"amplitude_mv","Pulse amplitude","mV (historical scale)"},
    {"baseline_mv","Baseline","mV (historical scale)"}, {"noise_mv","Baseline noise","mV (historical scale)"},
    {"cfd_ns","CFD arrival time","ns"}, {"fwhm_ns","Pulse FWHM","ns"},
    {"area","Main-pulse area","ADC sample"}, {"tail_negative_pc","Negative-tail area","pC (historical scale)"},
    {"peak_ns","Peak time","ns"}, {"threshold_ns","Threshold arrival time","ns"},
    {"rise_ns","Rise time (10-90%)","ns"}, {"fall_ns","Fall time (90-10%)","ns"},
    {"wave_dt_ns","Waveform-trigger offset","ns"}, {"pulses","Detected pulse multiplicity","Pulses"},
    {"window_ns","Integration window","ns"}, {"snr","Signal-to-noise ratio","Ratio"},
    {"amplitude","Pulse amplitude in native units","ADC"},
    {"baseline","Baseline in native units","ADC"},
    {"noise","Baseline noise in native units","ADC"},
    {"tail_negative","Negative-tail area in native units","ADC sample"}};
TCanvas *canvas; TFile *source,*focus,*readback; std::string output,string_label;
std::string shared_x,shared_y;
std::vector<TPad*> panels; std::vector<std::string> pages,main_pages;
std::ofstream caption_file; int columns=2;
const double width_pt=18.3/2.54*72, height_pt=18.3/2.54*72*1400/1200.;
int colors[8];
std::map<std::string,TH1*> loaded_histograms;
std::map<std::string,std::pair<int,double>> native_definitions;
bool sparse_preview=false;
std::string escape(std::string text) {
    std::string result;
    for(char c:text) {if(c=='"'||c=='\\')result+='\\';if(c=='\n')result+="\\n";else result+=c;}
    return result;
}
TLatex* text(double x,double y,const std::string &value,int size=20,int color=1) {
    auto t=new TLatex(x,y,value.c_str());t->SetNDC();t->SetTextFont(43);t->SetTextSize(size);t->SetTextColor(color);t->Draw();return t;
}
void axes(TAxis *x,TAxis *y,const std::string &xt,const std::string &yt) {
    for(auto a:{x,y}) {a->SetLabelFont(43);a->SetLabelSize(22);a->SetTitleFont(43);a->SetTitleSize(19);a->SetNdivisions(404);a->SetNoExponent(true);}
    x->SetLabelOffset(.04);y->SetLabelOffset(.02);
    x->SetTitle("");y->SetTitle("");shared_x=xt;shared_y=yt;
}
void begin_figure(const std::string &title,int count=8,int cols=2) {
    canvas->Clear();for(auto& item:loaded_histograms)delete item.second;loaded_histograms.clear();native_definitions.clear();sparse_preview=false;
    canvas->cd();panels.clear();columns=cols;shared_x.clear();shared_y.clear();
    text(.055,.962,string_label+" | "+title,28);
    auto body=new TPad("body","",.025,.14,.98,.88);body->SetFillStyle(0);body->SetBorderMode(0);body->SetBorderSize(0);body->Draw();body->Divide(cols,(count+cols-1)/cols,.012,.018);
    for(int i=0;i<count;++i) {
        auto pad=(TPad*)body->cd(i+1);pad->SetFillStyle(0);pad->SetBorderMode(0);pad->SetBorderSize(0);pad->SetLeftMargin(.17);pad->SetRightMargin(.055);
        pad->SetTopMargin(.27);pad->SetBottomMargin(.20);pad->SetTicks(1,1);panels.push_back(pad);
    }
}
void geometry(const std::string &name) {
    std::ofstream f(output+"/qa/"+name+".layout.json");
    f<<"{\"schema_version\":1,\"backend\":\"cpp-root\",\"figure\":{\"width_pt\":"<<width_pt<<",\"height_pt\":"<<height_pt<<"},\"panels\":[";
    for(size_t i=0;i<panels.size();++i) {
        if(i)f<<",";auto p=panels[i];double x=p->GetAbsXlowNDC(),y=p->GetAbsYlowNDC(),w=p->GetAbsWNDC(),h=p->GetAbsHNDC();
        f<<"{\"id\":\""<<char('a'+i)<<"\",\"bbox_pt\":["<<(x+w*p->GetLeftMargin())*width_pt<<","<<(y+h*p->GetBottomMargin())*height_pt
         <<","<<(x+w*(1-p->GetRightMargin()))*width_pt<<","<<(y+h*(1-p->GetTopMargin()))*height_pt<<"],\"grid_id\":\"root\",\"row_start\":"<<i/columns
         <<",\"row_stop\":"<<i/columns+1<<",\"col_start\":"<<i%columns<<",\"col_stop\":"<<i%columns+1
         <<",\"panel_label\":\""<<char('a'+i)<<"\",\"panel_label_anchor_pt\":["<<(x+.025*w)*width_pt<<","<<(y+.91*h)*height_pt<<"]}";
    }
    f<<"]}";
}
void save(const std::string &name,const std::string &caption,bool primary=false) {
    for(size_t i=0;i<panels.size();++i) {panels[i]->cd();text(.025,.91,std::string(1,'a'+i),23)->SetTextFont(63);}
    canvas->cd();
    if(!shared_x.empty())text(.42,.127,shared_x,19);
    if(!shared_y.empty()){auto t=new TLatex(.033,.49,shared_y.c_str());t->SetNDC();t->SetTextFont(43);t->SetTextSize(19);t->SetTextAngle(90);t->Draw();}
    std::string figure_caption=caption+(sparse_preview?" Display uses an occupied-bin envelope; every native bin and count is retained exactly in ROOT and tables.":"");
    std::istringstream words(figure_caption);std::string word,line;int row=0;
    while(words>>word) {
        if(line.size()+word.size()+1>112) {text(.055,.097-row*.024,line,17);++row;line.clear();}
        if(!line.empty())line+=' ';line+=word;
    }
    if(!line.empty())text(.055,.097-row*.024,line,17);
    canvas->Modified();canvas->Update();geometry(name);
    canvas->Print((output+"/png/"+name+".png").c_str());canvas->cd();
    canvas->Print((output+"/svg/"+name+".svg").c_str());canvas->cd();
    canvas->Print((output+"/pdf/"+name+".pdf").c_str());canvas->cd();
    canvas->Print((output+"/pdf/diagnostics.pdf").c_str());
    if(primary) {canvas->cd();canvas->Print((output+"/pdf/summary.pdf").c_str());main_pages.push_back(name);}
    pages.push_back(name);caption_file<<name<<"\t"<<figure_caption<<"\n";
}
TH1* histogram(TFile *file,const std::string &name) {
    auto found=loaded_histograms.find(name);if(found!=loaded_histograms.end())return found->second;
    auto key=file->GetKey(name.c_str());if(!key)return nullptr;
    std::unique_ptr<TObject> object(key->ReadObj());
    auto h=dynamic_cast<TH1*>(object.get());
    if(h){h->SetDirectory(nullptr);loaded_histograms[name]=h;object.release();return h;}
    auto sparse=dynamic_cast<THnSparse*>(object.get());if(!sparse)return nullptr;
    auto axis=sparse->GetAxis(0);native_definitions[name]={axis->GetNbins(),axis->GetBinWidth(1)};
    std::map<int,std::pair<int,double>> envelope;double below=0,above=0;Int_t coordinate[1];
    for(Long64_t i=0;i<sparse->GetNbins();++i){double n=sparse->GetBinContent(i,coordinate);int k=coordinate[0];
        if(k==0){below+=n;continue;}if(k==axis->GetNbins()+1){above+=n;continue;}if(!n)continue;
        int bucket=std::min(4095,int((axis->GetBinCenter(k)-axis->GetXmin())*4096/(axis->GetXmax()-axis->GetXmin())));
        auto old=envelope.find(bucket);if(old==envelope.end()||n>old->second.second)envelope[bucket]={k,n};
    }
    std::map<int,double> retained;std::set<double> edges{axis->GetXmin(),axis->GetXmax()};
    for(auto &item:envelope){int k=item.second.first;retained[k]=item.second.second;edges.insert(axis->GetBinLowEdge(k));edges.insert(axis->GetBinUpEdge(k));}
    std::vector<double> boundaries(edges.begin(),edges.end());
    h=new TH1D((name+"_display").c_str(),"",boundaries.size()-1,boundaries.data());h->SetDirectory(nullptr);
    for(auto &item:retained)h->SetBinContent(h->FindBin(axis->GetBinCenter(item.first)),item.second);
    h->SetBinContent(0,below);h->SetBinContent(h->GetNbinsX()+1,above);h->SetEntries(sparse->GetEntries());
    loaded_histograms[name]=h;sparse_preview=true;return h;
}
TGraph* graph(const std::string &name) {
    auto g=dynamic_cast<TGraph*>(source->Get(name.c_str()));
    return g?g:(readback?dynamic_cast<TGraph*>(readback->Get(name.c_str())):nullptr);
}
void line_graph(TGraph *g,const std::string &xt,const std::string &yt,int color) {
    if(!g || g->GetN()==0) {text(.25,.5,"No valid values",20);return;}
    g->SetTitle("");g->SetLineColor(color);g->SetLineWidth(2);g->Draw("AL");axes(g->GetXaxis(),g->GetYaxis(),xt,yt);
}
std::vector<std::vector<std::string>> read_csv(const std::string &path) {
    std::ifstream f(path);std::string line;std::vector<std::vector<std::string>> rows;
    while(std::getline(f,line)) {std::vector<std::string> row;std::istringstream s(line);std::string v;while(std::getline(s,v,','))row.push_back(v);rows.push_back(row);}
    return rows;
}
}

void render_analysis(const char *source_path,const char *focus_path,const char *target,const char *group_name,
                     Long64_t events,Long64_t waves,Long64_t accepted,int segments=1,int stride=10) {
    using namespace seastar_report;
    gROOT->SetBatch(true);TH1::AddDirectory(false);gStyle->SetOptStat(0);gStyle->SetOptTitle(0);
    gStyle->SetCanvasColor(0);gStyle->SetPadColor(0);gStyle->SetFrameLineWidth(1);gStyle->SetPalette(kViridis);
    gStyle->SetPaperSize(18.3,18.3*1400/1200.);
    const char *hex[]={"#3876A0","#BA7437","#438A79","#9B5871","#726BA6","#4895A4","#8B873B","#595D68"};
    for(int i=0;i<8;++i)colors[i]=TColor::GetColor(hex[i]);
    output=target;string_label=group_name;pages.clear();main_pages.clear();caption_file.open(output+"/captions.tsv");
    source=TFile::Open(source_path,"READ");focus=TFile::Open(focus_path,"READ");
    readback=gSystem->AccessPathName((output+"/root/readback.root").c_str())?nullptr:TFile::Open((output+"/root/readback.root").c_str(),"READ");
    if(!source||!focus||source->IsZombie()||focus->IsZombie())throw std::runtime_error("Cannot open analysis inputs");
    canvas=new TCanvas("optimized_report","",1200,1400);
    canvas->Print((output+"/pdf/summary.pdf[").c_str());canvas->Print((output+"/pdf/diagnostics.pdf[").c_str());
    auto quality=read_csv(output+"/tables/quality.csv");
    begin_figure("Coverage and waveform acceptance",2,2);
    TH1D count("count","",8,-.5,7.5), fraction("fraction","",8,-.5,7.5);
    for(size_t r=1;r<quality.size();++r)if(quality[r].size()>3) {
        int ch=std::stoi(quality[r][1]);double n=std::stod(quality[r][2]),good=std::stod(quality[r][3]);
        count.SetBinContent(ch+1,n/1e6);fraction.SetBinContent(ch+1,100*good/std::max(1.,n));
    }
    panels[0]->cd();count.SetFillColor(colors[0]);count.SetBarWidth(.7);count.SetBarOffset(.15);count.Draw("bar2");axes(count.GetXaxis(),count.GetYaxis(),"Channel","Waveforms (million)");
    panels[1]->cd();fraction.SetFillColor(colors[0]);fraction.SetBarWidth(.7);fraction.SetBarOffset(.15);fraction.SetMinimum(0);fraction.SetMaximum(100);fraction.Draw("bar2");axes(fraction.GetXaxis(),fraction.GetYaxis(),"Channel","Accepted waveforms (%)");
    panels[0]->cd();text(.17,.91,"Waveforms (million)",18);panels[1]->cd();text(.17,.91,"Accepted waveforms (%)",18);shared_y.clear();
    canvas->cd();text(.055,.915,"Saved events: "+std::to_string(events)+"   Waveforms: "+std::to_string(waves)+"   Accepted: "+std::to_string(accepted),19);
    save("overview","Acceptance follows the saved quality mask. Counts describe waveforms and saved events, not independent replicates or detector efficiency.",true);
    int metric_index=0;
    for(const auto &m:metrics) {
      bool show_complete=m.key=="charge_pc" || m.key=="amplitude" || m.key=="amplitude_mv";
      for(int complete_range=0;complete_range<=int(show_complete);++complete_range) {
      for(int logy=0;logy<=int(m.key=="charge_pc" || m.key=="amplitude" || m.key=="amplitude_mv");++logy) {
        std::string prefix=complete_range?"full_":"";
        begin_figure(m.title+(complete_range?" | complete range":" | local zoom")+(logy?" | logY":""));
        for(int ch=0;ch<8;++ch) {
            panels[ch]->cd();if(logy)panels[ch]->SetLogy();auto all=histogram(focus,prefix+"all_"+m.key+"_ch"+std::to_string(ch));auto good=histogram(focus,prefix+"accepted_"+m.key+"_ch"+std::to_string(ch));
            if(!all||!good)throw std::runtime_error("Missing focused histogram");
            auto a=(TH1*)all->Clone();auto g=(TH1*)good->Clone();a->SetDirectory(nullptr);g->SetDirectory(nullptr);a->SetBit(kCanDelete);g->SetBit(kCanDelete);
            a->SetLineColor(TColor::GetColor("#A6A9AC"));a->SetLineWidth(2);g->SetLineColor(colors[ch]);g->SetLineWidth(2);
            a->SetMinimum(logy?.5:0);a->SetMaximum(std::max(1.,a->GetMaximum()*1.15));a->Draw("hist");g->Draw("hist same");axes(a->GetXaxis(),a->GetYaxis(),m.unit,"Waveforms / bin");
            if(logy){a->GetYaxis()->SetLabelSize(19);a->GetYaxis()->SetNdivisions(303);}else a->GetYaxis()->ChangeLabel(1,-1,0);
            double n=good->GetEntries(),outside=good->GetBinContent(0)+good->GetBinContent(good->GetNbinsX()+1);
            std::ostringstream label;label<<"CH "<<ch;
            if(complete_range) {
                if(all->GetBinContent(0)!=0 || all->GetBinContent(all->GetNbinsX()+1)!=0 || outside!=0)
                    throw std::runtime_error("Complete-range histogram has overflow");
                label<<" | all "<<static_cast<Long64_t>(all->GetEntries())<<" | accepted "<<static_cast<Long64_t>(n);
            } else label<<" | outside "<<std::fixed<<std::setprecision(1)<<(n?outside/n*100:0)<<"%";
            text(.17,.91,label.str(),complete_range?17:18);
        }
        canvas->cd();text(.57,.915,"Grey: all waveforms   Colour: accepted",18);
        auto definition=histogram(focus,prefix+"all_"+m.key+"_ch0");std::ostringstream bin_label;
        auto native_definition=native_definitions.find(prefix+"all_"+m.key+"_ch0");
        bin_label<<(native_definition==native_definitions.end()?definition->GetNbinsX():native_definition->second.first)<<" bins | width "<<std::setprecision(6)<<(native_definition==native_definitions.end()?definition->GetBinWidth(1):native_definition->second.second);
        bool converted_integral=m.key=="charge_pc" || m.key=="tail_negative_pc";
        bool converted_adc=m.key=="amplitude_mv" || m.key=="baseline_mv" || m.key=="noise_mv";
        bin_label<<(converted_integral?" pC (1 ADC sample)":converted_adc?" mV (1 ADC)":" "+m.unit);text(.055,.915,bin_label.str(),18);
        std::string caption=m.key=="charge_pc"?
            "Bins were fixed at 1 ADC sample before conversion to pC. Axis calibration preserves every bin count; outside-view fractions use finite accepted values, and tables retain all range and missing counts.":
            converted_integral?"Negative-tail bins were fixed at 1 ADC sample before conversion to pC. Every bin count is preserved; this is an independent diagnostic, not an added charge correction.":
            converted_adc?"Bins were fixed at 1 ADC before conversion to mV. Axis calibration preserves every bin count; outside-view fractions use finite accepted values, and tables retain all range and missing counts.":
            "Binning is fixed in native units before any display-unit conversion. Outside-view fractions use finite accepted values; tables retain all range and missing counts.";
        if(logy)caption+=" LogY changes display only; zero-count bins remain zero in ROOT.";
        if(complete_range)caption="The axis covers the complete range of all finite waveforms, including quality-rejected values. Native bin widths and per-bin counts are unchanged; grey is all waveforms and colour is the accepted subset."+(logy?std::string(" Zero-count bins remain zero in ROOT."):std::string());
        bool primary=metric_index<6 && (show_complete?(complete_range && logy):!logy);
        save((complete_range?"full_channels_":"good_")+m.key+(logy?"_logy":""),caption,primary);
      }
      }
        begin_figure(m.title+" over the full range",1,1);auto pad=panels[0];pad->cd();pad->SetLogy();
        std::vector<TH1*> spectra;double lo=INFINITY,hi=-INFINITY,peak=0;
        for(int ch=0;ch<8;++ch) {
            auto original=histogram(focus,"full_accepted_"+m.key+"_ch"+std::to_string(ch));
            if(!original||original->GetEntries()==0)continue;
            auto h=(TH1*)original->Clone();h->SetDirectory(nullptr);h->SetBit(kCanDelete);h->Scale(1./original->GetEntries());h->SetLineColor(colors[ch]);h->SetLineWidth(2);
            lo=std::min(lo,h->GetXaxis()->GetXmin());hi=std::max(hi,h->GetXaxis()->GetXmax());peak=std::max(peak,h->GetMaximum());spectra.push_back(h);
        }
        if(!spectra.empty()) {
            auto frame=pad->DrawFrame(lo,std::max(1e-12,peak*1e-7),hi,peak*1.8);axes(frame->GetXaxis(),frame->GetYaxis(),m.unit,"Probability / bin");
            frame->GetYaxis()->SetNoExponent(false);
            auto legend=new TLegend(.12,.90,.95,.928);legend->SetNColumns(8);legend->SetBorderSize(0);legend->SetFillStyle(0);legend->SetTextFont(43);legend->SetTextSize(18);
            int i=0;for(auto h:spectra) {h->Draw("hist same");std::string channel=std::string(h->GetName()).substr(std::string(h->GetName()).rfind("_ch")+3);legend->AddEntry(h,("CH "+channel).c_str(),"l");++i;}
            canvas->cd();legend->Draw();
        }
        save("full_"+m.key,"Each accepted spectrum retains its original native bins and is normalized per bin by its finite sample count. Unit conversion changes only horizontal coordinates, preserving the bin probabilities.");++metric_index;
    }
    const std::string trend_names[]={"rate","baseline_trend","noise_trend","area_trend","good_fraction"};
    const std::string trend_titles[]={"Waveform rate","Baseline stability","Noise stability","Main-pulse area stability","Acceptance over time"};
    const std::string trend_units[]={"Waveforms / s","ADC","ADC","ADC sample","Accepted (%)"};
    for(int seg=0;seg<segments;++seg) for(int k=0;k<5;++k) {
        begin_figure(trend_titles[k]+" | electronic-clock segment "+std::to_string(seg));
        for(int ch=0;ch<8;++ch) {
            panels[ch]->cd();auto g=graph(trend_names[k]+"_ch"+std::to_string(ch)+"_segment"+std::to_string(seg));
            if(g) {auto copy=(TGraph*)g->Clone();copy->SetBit(kCanDelete);for(int i=0;i<copy->GetN();++i)copy->SetPointX(i,copy->GetPointX(i)/3600.);line_graph(copy,"Relative time (h)",trend_units[k],colors[ch]);}
            text(.17,.86,"CH "+std::to_string(ch),18);
        }
        save(trend_names[k]+"_segment"+std::to_string(seg),"Each string retains its own electronic-clock origin. Rates use observed bin exposure; area uses accepted waveforms, whereas baseline and noise use all waveforms. Clock segments are not joined.",k==0);
    }
    for(std::string kind:{"average","example","aligned"}) {
        begin_figure(kind=="average"?"Mean accepted waveforms":kind=="example"?"Example waveforms and integration limits":"Peak-aligned mean waveforms");
        for(int ch=0;ch<8;++ch) {
            panels[ch]->cd();TGraph *g=nullptr;
            if(kind=="average") {TIter next(source->GetListOfKeys());while(auto key=(TKey*)next())if(std::string(key->GetName()).find("average_ch"+std::to_string(ch)+"_n")==0){g=graph(key->GetName());break;}}
            else g=graph(kind=="example"?"example_ch"+std::to_string(ch)+"_0":"aligned_ch"+std::to_string(ch));
            line_graph(g,"Time (ns)",kind=="aligned"?"Baseline-subtracted ADC":"Raw ADC",colors[ch]);text(.17,.86,"CH "+std::to_string(ch),18);
            if(kind=="example"&&g) {
                auto window=(TNamed*)source->Get(("example_window_ch"+std::to_string(ch)+"_0").c_str());
                if(window) {
                    std::string v=window->GetTitle();std::replace(v.begin(),v.end(),',',' ');std::istringstream fields(v);double start,end,baseline;fields>>start>>end>>baseline;
                    double low=INFINITY,high=-INFINITY;for(int i=0;i<g->GetN();++i){low=std::min(low,g->GetPointY(i));high=std::max(high,g->GetPointY(i));}
                    for(double x:{start,end}){auto line=new TLine(x,low,x,high);line->SetLineColor(colors[ch]);line->SetLineStyle(2);line->Draw();}
                    auto line=new TLine(0,baseline,g->GetPointX(g->GetN()-1),baseline);line->SetLineColor(kGray+2);line->SetLineStyle(3);line->Draw();
                }
            }
        }
        save(kind+"_waveforms","Means use all accepted waveforms of the stored length. Examples are the first retained samples, not population estimates. Dashed limits identify the main integration window; dotted lines identify its baseline.",kind=="example");
    }
    begin_figure("Sampled raw-waveform density");
    double density_max=0;for(int ch=0;ch<8;++ch){auto h=histogram(source,"density_ch"+std::to_string(ch));if(h)density_max=std::max(density_max,h->GetMaximum());}
    double density_high=std::max(1.,std::ceil(std::log10(1+density_max)));
    for(int ch=0;ch<8;++ch) {
        panels[ch]->cd();panels[ch]->SetRightMargin(.17);auto original=(TH2*)source->Get(("density_ch"+std::to_string(ch)).c_str());
        if(original){auto h=(TH2*)original->Clone();h->SetDirectory(nullptr);h->SetBit(kCanDelete);
            for(int i=1;i<=h->GetNbinsX();++i)for(int j=1;j<=h->GetNbinsY();++j)h->SetBinContent(i,j,std::log10(1+original->GetBinContent(i,j)));
            h->SetMinimum(0);h->SetMaximum(density_high);h->Draw("colz");axes(h->GetXaxis(),h->GetYaxis(),"Fractional position","Raw ADC");h->GetZaxis()->SetLabelFont(43);h->GetZaxis()->SetLabelSize(19);h->GetZaxis()->SetNdivisions(303);h->GetZaxis()->SetNoExponent(true);}
        text(.17,.86,"CH "+std::to_string(ch),18);
    }
    save("waveform_density","Density uses every "+std::to_string(stride)+"th accepted waveform. Colour represents log10(1 + sample count), with one common scale within this string. Scalar metrics, spectra and means retain all applicable data.");
    begin_figure("Same-event association within this board",1,1);panels[0]->cd();panels[0]->SetRightMargin(.16);
    auto co=(TH2*)source->Get("coincidence");co->Draw("colz");axes(co->GetXaxis(),co->GetYaxis(),"Channel","Channel");co->GetZaxis()->SetLabelFont(43);co->GetZaxis()->SetLabelSize(22);co->GetZaxis()->SetNoExponent(true);
    save("coincidence","The diagonal counts events containing each channel; off-diagonal cells count same-event pairs within this board. No cross-string coincidence or clock alignment is performed.",true);
    begin_figure("Waveform-quality flags",1,1);panels[0]->cd();panels[0]->SetRightMargin(.16);panels[0]->SetBottomMargin(.30);
    TH2D flags("flag_matrix","",11,-.5,10.5,8,-.5,7.5);const char* labels[]={"Low SNR","Short baseline","Drift","Edge","Clipped","Pileup","No CFD","Secondary","ADC range","Fallback","Tail clipped"};
    for(int ch=0;ch<8;++ch){auto h=histogram(source,"flags_ch"+std::to_string(ch));double n=count.GetBinContent(ch+1)*1e6;for(int k=0;k<11;++k){flags.SetBinContent(k+1,ch+1,n&&h?h->GetBinContent(k+1)/n*100:0);flags.GetXaxis()->SetBinLabel(k+1,labels[k]);}}
    flags.Draw("colz");axes(flags.GetXaxis(),flags.GetYaxis(),"Quality flag","Channel");flags.GetXaxis()->LabelsOption("v");flags.GetZaxis()->SetLabelFont(43);flags.GetZaxis()->SetLabelSize(18);
    save("quality_flags","Percentages use all waveforms in each channel as their denominator. A waveform can carry several flags, so percentages need not sum to 100%.",true);
    for(std::string name:{"multiplicity","trigger_interval"}) {
        begin_figure(name=="multiplicity"?"Multiplicity of saved events":"Intervals between waveform events",1,1);panels[0]->cd();auto h=(TH1*)histogram(focus,"unit_"+name)->Clone();h->SetDirectory(nullptr);h->SetBit(kCanDelete);
        h->SetLineColor(colors[0]);h->SetLineWidth(2);h->Draw("hist");
        axes(h->GetXaxis(),h->GetYaxis(),name=="multiplicity"?"Waveforms per saved event":"log10(interval / s)","Saved events");
        save(name,"The input ROOT omits original empty triggers. These within-board saved-event statistics therefore do not define the total DAQ trigger rate.");
    }
    std::vector<std::string> pairs;TIter next(source->GetListOfKeys());while(auto key=(TKey*)next())if(std::string(key->GetName()).find("pair_")==0)pairs.push_back(key->GetName());
    for(size_t offset=0;offset<pairs.size();offset+=8) {
        int n=std::min(size_t(8),pairs.size()-offset);begin_figure("Same-event CFD differences within this board",n);
        for(int k=0;k<n;++k){panels[k]->cd();auto h=histogram(focus,"unit_"+pairs[offset+k]);if(!h)throw std::runtime_error("Missing unit-width within-board pair histogram");h->SetLineColor(colors[k]);h->SetLineWidth(2);h->Draw("hist");axes(h->GetXaxis(),h->GetYaxis(),"CFD difference (ns)","Events");h->GetYaxis()->ChangeLabel(1,-1,0);text(.17,.86,pairs[offset+k],18);}
        save("coincidence_cfd_"+std::to_string(offset/8),"Differences use the first accepted timing per channel within one saved event. No cross-string pairing or absolute-time matching is attempted.");
    }
    const std::vector<std::string> families[]={
        {"baseline_ch0","baseline_ch1","baseline_ch2","baseline_ch3","baseline_ch4","baseline_ch5","baseline_ch6","baseline_ch7"},
        {"hit_rate_ch0","hit_rate_ch1","hit_rate_ch2","hit_rate_ch3","hit_rate_ch4","hit_rate_ch5","hit_rate_ch6","hit_rate_ch7"},
        {"fpga_temperature_c","optical_module_reset_count","clock_chip_reset_count"}};
    const std::string family_names[]={"slow_baseline","slow_hit_rate","slow_diagnostics"};
    const std::string family_titles[]={"Readback baseline","Readback hit-rate indicator","Temperature and reset counters"};
    for(int k=0;k<3;++k) {
        bool available=false;for(auto field:families[k])if(graph(field+"_trend"))available=true;if(!available)continue;
        begin_figure(family_titles[k],families[k].size());
        for(size_t j=0;j<families[k].size();++j) {
            panels[j]->cd();auto g=graph(families[k][j]+"_trend");line_graph(g,"Beijing time (UTC+8)",families[k][j]=="fpga_temperature_c"?"Temperature (deg C)":"Readback value",colors[j]);
            if(g) {auto a=g->GetXaxis();a->SetTimeDisplay(true);a->SetTimeFormat("%m-%d %Hh");a->SetTimeOffset(8*3600,"gmt");a->SetNdivisions(303);}
            text(.17,.86,k<2?"CH "+std::to_string(j):j==0?"FPGA temperature":j==1?"Module resets":"Clock-chip resets",18);
        }
        save(family_names[k],"Readback trends retain the stored min/max envelope of the complete CSV window. Their Beijing-time axis remains separate from the waveform electronic-clock axis; no joint temporal analysis is performed.",k==2);
    }
    canvas->cd();canvas->Print((output+"/pdf/summary.pdf]").c_str());canvas->Print((output+"/pdf/diagnostics.pdf]").c_str());caption_file.close();
    std::ofstream manifest(output+"/rendering.json");manifest<<"{\"backend\":\"cpp-root\",\"figures\":[";
    for(size_t i=0;i<pages.size();++i){if(i)manifest<<",";manifest<<"\""<<escape(pages[i])<<"\"";}
    manifest<<"],\"overview_pages\":[";for(size_t i=0;i<main_pages.size();++i){if(i)manifest<<",";manifest<<"\""<<main_pages[i]<<"\"";}
    manifest<<"],\"summary_pages\":"<<main_pages.size()<<",\"complete_pages\":"<<pages.size()<<"}";
    canvas->Clear();for(auto& item:loaded_histograms)delete item.second;loaded_histograms.clear();
    source->Close();focus->Close();if(readback)readback->Close();delete canvas;canvas=nullptr;
}
