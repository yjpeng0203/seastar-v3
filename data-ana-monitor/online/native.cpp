#include "adaptive_waveform.h"
extern "C" void seastar_analyze(const uint16_t* samples,int n,
                                const seastar::Config* config,seastar::Pulse* result) {
    *result=seastar::analyze(samples,n,*config);
}
extern "C" void seastar_analyze_batch(const uint16_t* samples,const int* offsets,int count,
                                      const seastar::Config* config,seastar::Pulse* result) {
    for(int i=0;i<count;++i)
        result[i]=seastar::analyze(samples+offsets[i],offsets[i+1]-offsets[i],*config);
}
