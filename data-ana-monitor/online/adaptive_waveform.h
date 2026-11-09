#pragma once
// One numerical kernel shared by the online monitor and the historical ROOT macro.
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <vector>

namespace seastar {
enum Flag : uint32_t {
    LowSNR=1, BaselineShort=2, BaselineDrift=4, Edge=8, Clipped=16,
    Pileup=32, TimingMissing=64, Secondary=128, ADCRange=256,
    BaselineFallback=512, TailClipped=1024
};
struct Config {
    double sample_ns=2, snr=5, end_sigma=1.5, end_fraction=0.005,
           cfd_fraction=0.1, threshold_adc=4, timing_snr=3;
    int adc_max=4095, polarity=0, min_baseline=12, return_run=3, padding=2;
};
struct Pulse {
    double baseline=0, noise=0, amplitude=0, area=0, tail_negative=0,
           cfd_ns=NAN, threshold_ns=NAN, fwhm_ns=NAN, rise_ns=NAN,
           fall_ns=NAN, peak_ns=0;
    int start=0, end=0, peak=0, polarity=1, pulses=0, baseline_points=0;
    uint32_t flags=0;
};
inline double median(std::vector<double> x) {
    if (x.empty()) return 0;
    size_t k=x.size()/2;
    std::nth_element(x.begin(),x.begin()+k,x.end());
    double m=x[k];
    return x.size()%2 ? m : (m+*std::max_element(x.begin(),x.begin()+k))/2;
}
inline double robust_noise(const std::vector<double>& x) {
    if (x.size()<3) return 0.5;
    std::vector<double> d;
    for(size_t i=1;i<x.size();++i) d.push_back(x[i]-x[i-1]);
    double m=median(d);
    for(auto& v:d) v=std::abs(v-m);
    return std::max(0.5,1.4826*median(d)/std::sqrt(2.));
}
inline std::vector<double> smooth(const uint16_t* x,int n,double b,int sign) {
    std::vector<double> y(n);
    for(int i=0;i<n;++i)
        y[i]=sign*((x[std::max(0,i-1)]+double(x[i])+x[std::min(n-1,i+1)])/3-b);
    return y;
}
// Half-open [start,end); a run of quiet samples, rather than a fixed sample index.
inline int boundary(const std::vector<double>& y,int peak,int step,double low,const Config& c) {
    int quiet=0, n=int(y.size());
    for(int i=peak+step;i>=0&&i<n;i+=step) {
        quiet=y[i]<=low ? quiet+1 : 0;
        if(quiet>=c.return_run)
            return step<0 ? std::max(0,i-c.padding) : std::min(n,i+1+c.padding);
    }
    return step<0 ? 0 : n;
}
inline double crossing(const std::vector<double>& y,int lo,int hi,double level,bool rising) {
    for(int i=std::max(1,lo);i<=hi&&i<int(y.size());++i) {
        double a=y[i-1], b=y[i];
        if((rising ? a<level&&b>=level : a>level&&b<=level)&&b!=a)
            return i-1+(level-a)/(b-a);
    }
    return NAN;
}
inline Pulse analyze(const uint16_t* x,int n,const Config& c=Config()) {
    Pulse p;
    if(n<24) {p.flags=BaselineShort|LowSNR|TimingMissing; p.end=n; return p;}
    int seed=std::min(16,n/4);
    std::vector<double> pre(x,x+seed);
    double b=median(pre), noise=robust_noise(pre);
    double slope=std::abs(median(std::vector<double>(pre.begin(),pre.begin()+seed/2))-
                          median(std::vector<double>(pre.begin()+seed/2,pre.end())));
    if(slope>std::max(3*noise,2.)) {
        // No trustworthy prefix: locate a quiet block, but mark its provenance.
        double best=std::numeric_limits<double>::infinity();
        for(int k=0;k+seed<=n;k+=seed/2) {
            std::vector<double> w(x+k,x+k+seed);
            double m=median(w), s=0;
            if(m<=0||m>=c.adc_max) continue;
            for(auto v:w) s+=(v-m)*(v-m);
            s/=seed;
            if(s<best) {best=s; pre=w; b=m; noise=robust_noise(w);}
        }
        p.flags|=BaselineFallback;
    }
    auto y=smooth(x,n,b,1);
    double pos=*std::max_element(y.begin(),y.end()), neg=-*std::min_element(y.begin(),y.end());
    p.polarity=c.polarity ? c.polarity : (pos>=neg ? 1 : -1);
    for(auto& v:y) v*=p.polarity;
    int peak=std::max_element(y.begin(),y.end())-y.begin();
    double low=std::max({c.end_sigma*noise,c.end_fraction*y[peak],0.5});
    int left=boundary(y,peak,-1,low,c);
    std::vector<double> quiet;
    for(int i=0;i<left;++i) if(std::abs(x[i]-b)<=4*noise) quiet.push_back(x[i]);
    if(int(quiet.size())<c.min_baseline) {
        p.flags|=BaselineShort;
        quiet=pre;
    }
    // Sigma clipping removes precursors/outliers; use the mean for an unbiased DC estimate.
    double m=median(quiet), sum=0, sum2=0;
    int count=0;
    for(auto v:quiet) if(std::abs(v-m)<=4*noise) {sum+=v;sum2+=v*v;++count;}
    if(count) {b=sum/count;noise=std::max(0.5,std::sqrt(std::max(0.,sum2/count-b*b)));}
    if(count<c.min_baseline) p.flags|=BaselineShort;
    p.baseline=b; p.noise=noise; p.baseline_points=count;
    y=smooth(x,n,b,p.polarity);
    peak=std::max_element(y.begin(),y.end())-y.begin();
    // Locate the raw maximum close to the smoothed maximum (timing uses raw samples).
    for(int i=std::max(0,peak-1),end=std::min(n-1,peak+1);i<=end;++i)
        if(p.polarity*(x[i]-b)>p.polarity*(x[peak]-b)) peak=i;
    p.peak=peak; p.peak_ns=peak*c.sample_ns;
    p.amplitude=p.polarity*(x[peak]-b);
    low=std::max({c.end_sigma*noise,c.end_fraction*p.amplitude,0.5});
    p.start=boundary(y,peak,-1,low,c); p.end=boundary(y,peak,1,low,c);
    if(p.amplitude<c.snr*noise) p.flags|=LowSNR;
    if(p.start==0||p.end==n) p.flags|=Edge;
    if(left>=2*c.min_baseline) {
        int half=left/2; double a=0,z=0;
        for(int i=0;i<half;++i) a+=x[i];
        for(int i=half;i<2*half;++i) z+=x[i];
        if(std::abs(a-z)/half>std::max(4*noise,3.)) p.flags|=BaselineDrift;
    }
    for(int i=0;i<n;++i) if(x[i]>c.adc_max) p.flags|=ADCRange;
    for(int i=p.start;i<p.end;++i) {
        p.area+=p.polarity*(x[i]-b);
        if(x[i]>=c.adc_max||(p.polarity<0&&x[i]==0)) p.flags|=Clipped;
    }
    // Separate secondary peaks only when a real valley exists, not ADC noise on a plateau.
    std::vector<int> candidates;
    for(int i=1;i<n-1;++i)
        if(y[i]>=c.snr*noise&&y[i]>=y[i-1]&&y[i]>y[i+1]) candidates.push_back(i);
    std::sort(candidates.begin(),candidates.end(),[&](int a,int z){return y[a]>y[z];});
    std::vector<int> selected;
    for(int i:candidates) {
        bool separate=true;
        for(int j:selected) {
            int a=std::min(i,j),z=std::max(i,j);
            if(z-a<6||*std::min_element(y.begin()+a,y.begin()+z+1)>0.65*std::min(y[i],y[j]))
                {separate=false;break;}
        }
        if(separate) selected.push_back(i);
        if(selected.size()>=16) break;
    }
    p.pulses=int(selected.size());
    int inside=0;
    for(int i:selected) if(i>=p.start&&i<p.end) ++inside;
    if(inside>1) p.flags|=Pileup;
    if(p.pulses>1) p.flags|=Secondary;
    // Negative tail is a separate diagnostic, never silently added to the main area.
    double tail_level=std::max(3*noise,1.);
    int neg_run=0, tail_begin=-1;
    for(int i=p.end;i<n;++i) {
        if(y[i]>c.snr*noise) break; // stop before a later positive pulse
        neg_run=y[i]<-tail_level ? neg_run+1 : 0;
        if(neg_run>=c.return_run) {tail_begin=i-c.return_run+1;break;}
    }
    if(tail_begin>=0) {
        int tail_end=n, q=0;
        for(int i=tail_begin;i<n;++i) {
            q=y[i]>=-tail_level ? q+1 : 0;
            if(q>=c.return_run) {tail_end=i+1;break;}
        }
        for(int i=tail_begin;i<tail_end;++i) {
            p.tail_negative+=std::max(0.,-p.polarity*(x[i]-b));
            if(x[i]==0||x[i]>=c.adc_max) p.flags|=TailClipped;
        }
    }
    std::vector<double> raw(n);
    for(int i=0;i<n;++i) raw[i]=p.polarity*(x[i]-b);
    if(!(p.flags&LowSNR)) {
        double t10=p.amplitude*.1>=c.timing_snr*noise ? crossing(raw,p.start,peak,p.amplitude*.1,true) : NAN;
        double t90=crossing(raw,p.start,peak,p.amplitude*.9,true);
        double t50=p.amplitude*.5>=c.timing_snr*noise ? crossing(raw,p.start,peak,p.amplitude*.5,true) : NAN;
        double f90=crossing(raw,peak+1,p.end-1,p.amplitude*.9,false);
        double f50=crossing(raw,peak+1,p.end-1,p.amplitude*.5,false);
        double f10=p.amplitude*.1>=c.timing_snr*noise ? crossing(raw,peak+1,p.end-1,p.amplitude*.1,false) : NAN;
        // A crossing below the baseline noise is not a trustworthy interpolated arrival time.
        if(p.amplitude*c.cfd_fraction>=c.timing_snr*noise)
            p.cfd_ns=crossing(raw,p.start,peak,p.amplitude*c.cfd_fraction,true)*c.sample_ns;
        if(c.threshold_adc>=c.timing_snr*noise)
            p.threshold_ns=crossing(raw,p.start,peak,c.threshold_adc,true)*c.sample_ns;
        p.rise_ns=(t90-t10)*c.sample_ns; p.fall_ns=(f10-f90)*c.sample_ns;
        p.fwhm_ns=(f50-t50)*c.sample_ns;
    }
    if(!std::isfinite(p.cfd_ns)) p.flags|=TimingMissing;
    return p;
}
inline bool good(const Pulse& p) {
    return !(p.flags&(LowSNR|BaselineShort|Edge|Clipped|Pileup|ADCRange|BaselineFallback));
}
} // namespace seastar
