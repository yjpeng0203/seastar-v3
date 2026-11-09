// =============================================================================
// analyze_charge.C — SeaStar 波形数据分析（一次扫描完成全部计算）
// =============================================================================
//
// 【基本运行逻辑】
//   1. 读取 TTree "seastar"，遍历全部 Entry ，每个 Entry 包含若干波形
//   2. 使用 ../online/adaptive_waveform.h：自适应基线、主峰与半开积分窗口
//   3. 同时找峰值位置和幅度，填入对应直方图
//   4. 最后一次性画出所有图谱并保存
//
// 【功能清单】
//   - 电荷谱: 8 通道叠加 + 4×2 子图 + logY 版本
//   - 波形叠加: 热力图(COLZ) + 平均波形(红色线)，4×2 子图
//   - 峰位时间分布: 分通道 + 叠加 + logY（叠加图中标注各通道均值）
//   - 幅度分布: 分通道 + 叠加 + logY
//   - 时间演化: 电荷/基线/幅度 随真实时间的变化（分通道 + 叠加）
//   - 负向尾部: 独立诊断；默认不补偿。可选对比仅使用主窗之外的负尾面积
//
// 【重要开关】
//   enable_tail_correction  — 是否开启尾部积分修正
//   enable_threshold        — 是否开启幅度阈值筛选（在 low~high 区间外的波形被丢弃）
//   enable_time_evolution   — 是否画时间演化图
//   max_wf_sample           — 波形热力图抽样上限（0=画全部）
//
// 【单位换算】
//   1 sample = 2 ns, 1 ADC = 0.5 mV, 前端阻抗 = 50 Ω
//   Q [pC] = Σ(ADC-bl) × 0.5mV × 2ns ÷ 50Ω = Σ(ADC-bl) × 0.02
//
// 【使用方法】
//   交互模式: root -l seastar_stringtest/data_xxx.root
//             root [0] .x analyze_charge.C
//   批处理:   root -l -b -q 'seastar_stringtest/data_xxx.root' 'analyze_charge.C'
//
// 【输出目录】 results_YYYYMMDD_HHMMSS/  (自动带时间戳)
// =============================================================================
//
#include "../online/adaptive_waveform.h"
#include <stdexcept>
#include <TClass.h>
#include <TVirtualCollectionProxy.h>
// 用法: root -l seastar_stringtest/data_LED-8ns_20260623-24-IP58.root
//       .x analyze_charge.C
//  或: root -l -b -q 'seastar_stringtest/data_LED-8ns_20260623-24-IP58.root' 'analyze_charge.C'

// ────── 辅助函数: 计算半高宽 FWHM (线性插值) ──────
double GetFWHM(TH1D *h) {
    if (!h || h->GetEntries() == 0) return 0;
    int maxbin = h->GetMaximumBin();
    double halfmax = h->GetBinContent(maxbin) / 2.0;

    // 向左找半高点
    int ilow = maxbin;
    for (int i = maxbin; i >= 1; i--) {
        if (h->GetBinContent(i) < halfmax) break;
        ilow = i;
    }
    double xlow;
    if (ilow <= 1) {
        xlow = h->GetBinLowEdge(1);
    } else {
        double x1 = h->GetBinCenter(ilow - 1);
        double x2 = h->GetBinCenter(ilow);
        double y1 = h->GetBinContent(ilow - 1);
        double y2 = h->GetBinContent(ilow);
        xlow = x1 + (x2 - x1) * (halfmax - y1) / (y2 - y1);
    }

    // 向右找半高点
    int nbins = h->GetNbinsX();
    int ihigh = maxbin;
    for (int i = maxbin; i <= nbins; i++) {
        if (h->GetBinContent(i) < halfmax) break;
        ihigh = i;
    }
    double xhigh;
    if (ihigh >= nbins) {
        xhigh = h->GetBinLowEdge(nbins) + h->GetBinWidth(nbins);
    } else {
        double x1 = h->GetBinCenter(ihigh);
        double x2 = h->GetBinCenter(ihigh + 1);
        double y1 = h->GetBinContent(ihigh);
        double y2 = h->GetBinContent(ihigh + 1);
        xhigh = x1 + (x2 - x1) * (halfmax - y1) / (y2 - y1);
    }

    return xhigh - xlow;
}

void GetProfileRange(TProfile **profiles, int n, double &lo, double &hi) {
    lo = std::numeric_limits<double>::infinity(); hi = -lo;
    for (int ch = 0; ch < n; ch++)
        for (int ib = 1; ib <= profiles[ch]->GetNbinsX(); ib++)
            if (profiles[ch]->GetBinEntries(ib) > 0) {
                lo = std::min(lo, profiles[ch]->GetBinContent(ib));
                hi = std::max(hi, profiles[ch]->GetBinContent(ib));
            }
    if (!std::isfinite(lo)) { lo = 0; hi = 1; }
    double margin = std::max(1., (hi-lo)*0.1);
    lo -= margin; hi += margin;
}

void analyze_charge() {
    // ====== 配置区 ======
    // 输出目录（自动带时间戳，格式: results_20260710_143052）
    TDatime now;
    std::string outdir = TString::Format("results_%04d%02d%02d_%02d%02d%02d",
        now.GetYear(), now.GetMonth(), now.GetDay(),
        now.GetHour(), now.GetMinute(), now.GetSecond()).Data();

    int n_samples   = 504;

    // --- 电荷直方图 ---
    double q_min    = -200;     // 直方图 X 轴下限 [pC]
    double q_max    = 4000;     // 直方图 X 轴上限 [pC]
    int n_bins      = 420;     // bin 数

    // --- 波形热力图抽样(0=全部) ---
    int max_wf_sample = 0;     // 0 表示不抽样，画全部波形

    // --- 时间演化 ---
    bool enable_time_evolution = true;   // true=画电荷/基线/幅度随真实时间的演化
    double time_xmin = 0;                // 横轴下限 [s]
    double time_xmax = 60000;            // 横轴上限 [s]
    int time_bins   = 600;             // bin 数: 100 s/bin

    // --- 幅度阈值(0=关闭) ---
    bool enable_threshold = false;   // true=开启幅度阈值筛选
    //bool enable_threshold = true;   // true=开启幅度阈值筛选
    double ampl_threshold_low  = 300.0;   // 幅度下限 [mV]
    double ampl_threshold_high = 2000.0;  // 幅度上限 [mV]（设很大如 99999 则不限制上限）

    // --- 尾部积分修正(0=关闭) ---
    bool enable_tail_correction = false; // 默认不将负向尾部诊断量当作已验证补偿

    // --- Transit Time (渡越时间) ---
    double tt_threshold_mv = 2.0;     // 阈值法: baseline + 2 mV
    double tt_cfd_fraction = 0.10;    // CFD 恒比定时: 峰值的 10%
    double tt_min = 0;                // 波形内到达时间，非已标定的本征 TT
    double tt_max = n_samples * 2.;
    int tt_bins = n_samples;

    // --- 单位换算 ---
    double sample_ns  = 2.0;     // 1 sample = 2 ns
    double adc_mv     = 0.5;     // 1 ADC = 0.5 mV
    double resistance = 50.0;    // 前端输入阻抗 [Ω]
    seastar::Config pulse_config;
    pulse_config.sample_ns = sample_ns;
    pulse_config.threshold_adc = tt_threshold_mv / adc_mv;
    pulse_config.cfd_fraction = tt_cfd_fraction;
    // Q [pC] = Σ(ADC-bl) × 0.5mV × 2ns ÷ 50Ω = Σ(ADC-bl) × 0.02
    // ==================

    // 同一秒重跑也不覆盖已有分析结果。
    std::string base_outdir = outdir;
    for (int suffix = 1; !gSystem->AccessPathName(outdir.c_str()); suffix++)
        outdir = base_outdir + "_" + std::to_string(suffix);
    // 创建输出目录
    gSystem->mkdir(outdir.c_str(), true);
    gSystem->mkdir((outdir + std::string("/png")).c_str(), true);
    gSystem->mkdir((outdir + std::string("/pdf")).c_str(), true);
    gSystem->mkdir((outdir + std::string("/root")).c_str(), true);

    TFile *f = (TFile*)gDirectory->GetFile();
    if (!f) { cerr << "Error: no file open" << endl; return; }

    // TTreeReader requires a compiled proxy for nested STL vectors on some ROOT installs.
    TClass *sample_class = TClass::GetClass("vector<vector<unsigned short> >");
    if (!sample_class || !sample_class->GetCollectionProxy() ||
        (sample_class->GetCollectionProxy()->GetProperties() & TVirtualCollectionProxy::kIsEmulated)) {
        std::string working_directory = gSystem->WorkingDirectory();
        std::string dictionary_directory = working_directory + "/" + outdir + "/root/dictionary";
        gSystem->mkdir(dictionary_directory.c_str(), true);
        if (!gSystem->ChangeDirectory(dictionary_directory.c_str()))
            throw std::runtime_error("Cannot create ROOT dictionary directory");
        int dictionary_ok = gInterpreter->GenerateDictionary("vector<vector<unsigned short> >", "vector");
        gSystem->ChangeDirectory(working_directory.c_str());
        if (!dictionary_ok) throw std::runtime_error("Cannot build nested-vector ROOT dictionary");
        f->cd();
    }

    TTreeReader reader("seastar", f);
    TTreeReaderValue<Int_t> r_n_waves(reader, "n_waves");
    TTreeReaderValue<UInt_t> r_trigger_second(reader, "trigger_second");
    TTreeReaderValue<UInt_t> r_trigger_nanosecond(reader, "trigger_nanosecond");
    TTreeReaderValue<vector<unsigned char>> r_ch_id(reader, "ch_id");
    TTreeReaderValue<vector<unsigned char>> r_hunt_id(reader, "hunt_id");
    TTreeReaderValue<vector<vector<unsigned short>>> r_samples(reader, "samples");
    TTree *input_tree = (TTree*)f->Get("seastar");
    if (!input_tree || input_tree->GetEntries() == 0) {
        cerr << "Error: empty or missing seastar tree" << endl;
        return;
    }
    n_samples = (int)input_tree->GetMaximum("waveform_points");
    if (n_samples < 24 || n_samples > 8192) throw std::runtime_error("Invalid waveform length");
    tt_max = n_samples * sample_ns;
    tt_bins = n_samples;

    const int nch = 8;
    int colors[] = {kBlack, kRed, kBlue, kGreen+2, kMagenta, kCyan+2, kOrange+7, kViolet+2};

    // ────── 初始化所有直方图 ──────
    // 电荷谱
    TH1D *h_q[nch];
    for (int ch = 0; ch < nch; ch++) {
        h_q[ch] = new TH1D(Form("h_q_ch%d", ch),
                           Form("Ch %d;Charge [pC];Entries", ch),
                           n_bins, q_min, q_max);
        h_q[ch]->SetLineColor(colors[ch]);
        h_q[ch]->SetLineWidth(2);
        h_q[ch]->SetStats(0);
    }

    // 尾部修正后的电荷谱（用于对比）
    TH1D *h_q_tail[nch];
    if (enable_tail_correction) {
        for (int ch = 0; ch < nch; ch++) {
            h_q_tail[ch] = new TH1D(Form("h_q_tail_ch%d", ch),
                                    Form("Ch %d (tail corr);Charge [pC];Entries", ch),
                                    n_bins, q_min, q_max);
            h_q_tail[ch]->SetLineColor(colors[ch]);
            h_q_tail[ch]->SetLineStyle(2);  // 虚线区分
            h_q_tail[ch]->SetLineWidth(2);
            h_q_tail[ch]->SetStats(0);
        }
    }

    // 波形热力图 + 平均波形
    TH2D *h2_wave[nch];
    TProfile *p_avg[nch];
    for (int ch = 0; ch < nch; ch++) {
        h2_wave[ch] = new TH2D(Form("h2_wave_ch%d", ch),
                               Form("Ch %d;Time [ns];Amplitude [mV];Entries", ch),
                               n_samples, 0, n_samples * sample_ns,
                               256, 0, 4096 * adc_mv);
        h2_wave[ch]->SetStats(0);

        p_avg[ch] = new TProfile(Form("p_avg_ch%d", ch),
                                 Form("Ch %d;Time [ns];Amplitude [mV]", ch),
                                 n_samples, 0, n_samples * sample_ns);
    }

    // 峰位时间 + 幅度
    double t_min = 0;
    double t_max = n_samples * sample_ns;
    int t_bins = n_samples;
    TH1D *h_peak_t[nch], *h_ampl[nch];
    for (int ch = 0; ch < nch; ch++) {
        h_peak_t[ch] = new TH1D(Form("h_peak_t_ch%d", ch),
                                Form("Ch %d;Peak Time [ns];Entries", ch),
                                t_bins, t_min, t_max);
        h_ampl[ch] = new TH1D(Form("h_ampl_ch%d", ch),
                              Form("Ch %d;Amplitude [mV];Entries", ch),
                              400, 0, 2000);
    }

    // 渡越时间 (TT): 阈值法 + CFD 法
    TH1D *h_tt_th[nch], *h_tt_cfd[nch];
    for (int ch = 0; ch < nch; ch++) {
        h_tt_th[ch] = new TH1D(Form("h_tt_th_ch%d", ch),
                               Form("Ch %d (threshold +2mV);Waveform arrival time [ns];Entries", ch),
                               tt_bins, tt_min, tt_max);
        h_tt_cfd[ch] = new TH1D(Form("h_tt_cfd_ch%d", ch),
                                Form("Ch %d (CFD %.0f%%);Waveform arrival time [ns];Entries",
                                     ch, tt_cfd_fraction * 100),
                                tt_bins, tt_min, tt_max);
    }

    int wf_count[nch] = {0};

    // ────── 主循环：只扫一次数据 ──────
    Long64_t nentries = reader.GetEntries(true);
    cout << "Processing " << nentries << " events..." << endl;
    if (enable_threshold)
        cout << "  Amplitude threshold: " << ampl_threshold_low << " ~ " << ampl_threshold_high << " mV" << endl;
    else
        cout << "  Amplitude threshold: OFF" << endl;

    // ────── 时间演化 TProfile ──────
    // 读取第一个和最后一个 Entry 获得时间和范围
    reader.SetEntry(0);
    double t0 = *r_trigger_second + *r_trigger_nanosecond * 1e-9;
    reader.SetEntry(nentries - 1);
    double t_last = *r_trigger_second + *r_trigger_nanosecond * 1e-9;
    double t_range = t_last - t0;  // 秒
    if (t_range < 0) throw std::runtime_error("Clock rollback: use online monitor time segments");
    time_xmax = std::max(1., std::ceil(t_range / 100.) * 100. + 1.);

    TProfile *p_q_vs_t[nch];    // 电荷 vs 时间
    TProfile *p_bl_vs_t[nch];   // 基线 vs 时间
    TProfile *p_ampl_vs_t[nch]; // 幅度 vs 时间
    TH1D *h_rate_vs_t[nch];     // 计数率 vs 时间

    if (enable_time_evolution) {
        cout << "Time axis: " << time_xmin << " ~ " << time_xmax << " s,  "
             << time_bins << " bins (" << (time_xmax-time_xmin)/time_bins << " s/bin)" << endl;
        for (int ch = 0; ch < nch; ch++) {
            p_q_vs_t[ch] = new TProfile(Form("p_q_vs_t_ch%d", ch),
                                        Form("Ch %d Charge vs Time;Time [s];Charge [pC]", ch),
                                        time_bins, time_xmin, time_xmax);
            p_bl_vs_t[ch] = new TProfile(Form("p_bl_vs_t_ch%d", ch),
                                         Form("Ch %d Baseline vs Time;Time [s];Baseline [ADC]", ch),
                                         time_bins, time_xmin, time_xmax);
            p_ampl_vs_t[ch] = new TProfile(Form("p_ampl_vs_t_ch%d", ch),
                                           Form("Ch %d Amplitude vs Time;Time [s];Amplitude [mV]", ch),
                                           time_bins, time_xmin, time_xmax);
            h_rate_vs_t[ch] = new TH1D(Form("h_rate_vs_t_ch%d", ch),
                                       Form("Ch %d Count Rate;Time [s];Count Rate [Hz]", ch),
                                       time_bins, time_xmin, time_xmax);
        }
    }

    long long raw_wave_count = 0, rejected_wave_count = 0, amplitude_rejected_wave_count = 0;
    int input_board = -1;
    double previous_trigger_t = t0;
    for (Long64_t evt = 0; evt < nentries; evt++) {
        reader.SetEntry(evt);
        if (evt % 100000 == 0)
            cout << "  Event " << evt << " / " << nentries << endl;

        const auto &ch_id = *r_ch_id;
        const auto &samples = *r_samples;
        const auto &hunt_id = *r_hunt_id;
        if (samples.size() != ch_id.size() || samples.size() != hunt_id.size() ||
            (int)samples.size() != *r_n_waves) throw std::runtime_error("Mismatched waveform arrays");
        double current_trigger_t = *r_trigger_second + *r_trigger_nanosecond * 1e-9;
        if (current_trigger_t < previous_trigger_t) throw std::runtime_error("Clock rollback: use online monitor time segments");
        previous_trigger_t = current_trigger_t;

        for (size_t i = 0; i < samples.size(); i++) {
            int ch = ch_id[i];
            if (ch < 0 || ch >= nch) throw std::runtime_error("Channel outside legacy 8-channel plots: use online monitor");
            if (input_board < 0) input_board = hunt_id[i];
            if (input_board != hunt_id[i]) throw std::runtime_error("Multiple boards: use online monitor, which separates board/channel");

            const auto &wave = samples[i];
            int npts = (int)wave.size();
            raw_wave_count++;
            seastar::Pulse pulse = seastar::analyze(wave.data(), npts, pulse_config);
            if (!seastar::good(pulse)) { rejected_wave_count++; continue; }
            double baseline = pulse.baseline;
            double ampl_mv = pulse.amplitude * adc_mv;

            // ─── 幅度阈值筛选 ───
            if (enable_threshold && (ampl_mv <= ampl_threshold_low || ampl_mv >= ampl_threshold_high)) {
                amplitude_rejected_wave_count++;
                continue;
            }

            // ─── 时间演化: 当前事件的相对时间 ───
            double evt_time = 0;
            if (enable_time_evolution) {
                double trigger_t = *r_trigger_second + *r_trigger_nanosecond * 1e-9;
                evt_time = trigger_t - t0;  // 秒
            }

            // ─── (A) 电荷: mV·ns → pC ───
            double charge = pulse.area;
            // conversion: ADC × 0.5mV × 2ns ÷ 50Ω = ADC/50 pC
            charge = charge * adc_mv * sample_ns / resistance;  // pC
            h_q[ch]->Fill(charge);

            // ─── (A2) 尾部积分修正 ───
            if (enable_tail_correction) {
                double tail_charge = pulse.tail_negative;
                // conversion: ADC × 0.5mV × 2ns ÷ 50Ω = ADC/50 pC
                tail_charge = tail_charge * adc_mv * sample_ns / resistance;
                double charge_corrected = charge + tail_charge;
                h_q_tail[ch]->Fill(charge_corrected);
            }

            // ─── 填入时间演化 TProfile ───
            if (enable_time_evolution) {
                p_q_vs_t[ch]->Fill(evt_time, charge);
                p_bl_vs_t[ch]->Fill(evt_time, baseline);
                p_ampl_vs_t[ch]->Fill(evt_time, ampl_mv);
                h_rate_vs_t[ch]->Fill(evt_time);
            }

            // ─── (B) 热力图 + 平均波形 ───
            bool fill_h2 = (max_wf_sample == 0 || wf_count[ch] < max_wf_sample);
            if (fill_h2) {
                for (int j = 0; j < npts; j++) {
                    double t_ns = j * sample_ns;
                    double amp_mv_pt = wave[j] * adc_mv;
                    h2_wave[ch]->Fill(t_ns, amp_mv_pt);
                    p_avg[ch]->Fill(t_ns, amp_mv_pt);
                }
            }

            // 填入峰位/幅度直方图
            double peak_t_ns = pulse.peak_ns;
            h_peak_t[ch]->Fill(peak_t_ns);
            h_ampl[ch]->Fill(ampl_mv);

            // ─── Transit Time (TT): 阈值法 — baseline + 2mV ───
            if (std::isfinite(pulse.threshold_ns)) h_tt_th[ch]->Fill(pulse.threshold_ns);

            // ─── Transit Time (TT): CFD 恒比定时 (线性插值亚采样) ───
            if (std::isfinite(pulse.cfd_ns)) h_tt_cfd[ch]->Fill(pulse.cfd_ns);

            wf_count[ch]++;
        }
    }

    cout << "Done! Drawing..." << endl;

    // ══════════ 画图：电荷 ══════════
    TCanvas *c1 = new TCanvas("c1", "Charge Spectrum - All Channels", 1200, 800);
    TLegend *leg1 = new TLegend(0.75, 0.7, 0.9, 0.9);
    bool drawn_q = false;
    for (int ch = 0; ch < nch; ch++) {
        if (h_q[ch]->GetEntries() == 0) continue;
        h_q[ch]->Draw(drawn_q ? "SAME" : "");
        drawn_q = true;
        leg1->AddEntry(h_q[ch], Form("Ch %d (%.0f)", ch, h_q[ch]->GetEntries()), "l");
    }
    leg1->Draw();
    c1->SaveAs((outdir + std::string("/png/") + "charge_all_channels.png").c_str());
    c1->SaveAs((outdir + std::string("/pdf/") + "charge_all_channels.pdf").c_str());
    c1->SetLogy();
    c1->SaveAs((outdir + std::string("/png/") + "charge_all_channels_logy.png").c_str());
    c1->SaveAs((outdir + std::string("/pdf/") + "charge_all_channels_logy.pdf").c_str());
            { TFile *fr_c1 = new TFile((outdir + std::string("/root/charge_all_channels.root")).c_str(), "RECREATE"); for (int ch = 0; ch < nch; ch++) h_q[ch]->Write(); fr_c1->Close(); }
    { TFile *fr = new TFile((outdir + std::string("/root/charge_all_channels.root")).c_str(), "RECREATE"); for (int ch = 0; ch < nch; ch++) h_q[ch]->Write(); fr->Close(); }

    TCanvas *c2 = new TCanvas("c2", "Charge per Channel", 1400, 800);
    c2->Divide(4, 2);
    for (int ch = 0; ch < nch; ch++) { c2->cd(ch + 1); h_q[ch]->Draw(); }
    c2->SaveAs((outdir + std::string("/png/") + "charge_per_channel.png").c_str());
    c2->SaveAs((outdir + std::string("/pdf/") + "charge_per_channel.pdf").c_str());
    for (int ch = 0; ch < nch; ch++) { c2->cd(ch + 1); gPad->SetLogy(); }
    c2->SaveAs((outdir + std::string("/png/") + "charge_per_channel_logy.png").c_str());
    c2->SaveAs((outdir + std::string("/pdf/") + "charge_per_channel_logy.pdf").c_str());
            { TFile *fr_c2 = new TFile((outdir + std::string("/root/charge_per_channel.root")).c_str(), "RECREATE"); for (int ch = 0; ch < nch; ch++) h_q[ch]->Write(); fr_c2->Close(); }

    // ══════════ 画图：尾部修正电荷对比 ══════════
    if (enable_tail_correction) {
        TCanvas *c3 = new TCanvas("c3", "Charge Comparison (Original vs Tail-Corrected)", 1400, 800);
        c3->Divide(4, 2);
        for (int ch = 0; ch < nch; ch++) {
            c3->cd(ch + 1);
            h_q[ch]->SetLineColor(kRed);
            h_q[ch]->SetLineWidth(2);
            h_q[ch]->Draw();
            h_q_tail[ch]->SetLineColor(kBlue);
            h_q_tail[ch]->SetLineWidth(2);
            h_q_tail[ch]->Draw("SAME");
            // 图例
            if (ch == 0) {
                TLegend *leg_cmp = new TLegend(0.6, 0.75, 0.9, 0.9);
                leg_cmp->AddEntry(h_q[ch], "Original", "l");
                leg_cmp->AddEntry(h_q_tail[ch], "Tail-corrected", "l");
                leg_cmp->Draw();
            }
        }
        c3->SaveAs((outdir + std::string("/png/") + "charge_comparison.png").c_str());
        c3->SaveAs((outdir + std::string("/pdf/") + "charge_comparison.pdf").c_str());
        c3->SetLogy();
        for (int ch = 0; ch < nch; ch++) { c3->cd(ch + 1); gPad->SetLogy(); }
        c3->SaveAs((outdir + std::string("/png/") + "charge_comparison_logy.png").c_str());
        c3->SaveAs((outdir + std::string("/pdf/") + "charge_comparison_logy.pdf").c_str());
            { TFile *fr_c3 = new TFile((outdir + std::string("/root/charge_comparison.root")).c_str(), "RECREATE"); for (int ch = 0; ch < nch; ch++) { h_q[ch]->Write(); h_q_tail[ch]->Write(); } fr_c3->Close(); }
        cout << "Saved charge_comparison.png / .pdf" << endl;
    }

    // ══════════ 画图：波形叠加 ══════════
    TCanvas *cw = new TCanvas("cw", "Waveforms per Channel", 1400, 1000);
    cw->Divide(2, 4);
    for (int ch = 0; ch < nch; ch++) {
        cw->cd(ch + 1)->SetLogz();
        gPad->SetGridx();
        gPad->SetGridy();
        gPad->SetLeftMargin(0.10);
        gPad->SetRightMargin(0.12);
        gPad->SetBottomMargin(0.12);
        gPad->SetTopMargin(0.12);
        int n_wf_shown = (max_wf_sample == 0) ? wf_count[ch]
                                             : std::min(wf_count[ch], max_wf_sample);
        h2_wave[ch]->SetTitle(Form("Ch %d (%d waveforms);Time [ns];Amplitude [mV]",
                                   ch, n_wf_shown));
        h2_wave[ch]->GetXaxis()->SetTitleSize(0.07);
        h2_wave[ch]->GetYaxis()->SetTitleSize(0.07);
        h2_wave[ch]->GetXaxis()->SetLabelSize(0.05);
        h2_wave[ch]->GetYaxis()->SetLabelSize(0.05);
        h2_wave[ch]->GetXaxis()->SetTitleOffset(0.7);
        h2_wave[ch]->GetYaxis()->SetTitleOffset(0.6);
        h2_wave[ch]->GetZaxis()->SetTitleSize(0.07);
        h2_wave[ch]->GetZaxis()->SetLabelSize(0.06);
        h2_wave[ch]->GetZaxis()->SetTitleOffset(0.55);
        h2_wave[ch]->Draw("COLZ");
        p_avg[ch]->SetLineColor(kRed);
        p_avg[ch]->SetLineWidth(1);
        p_avg[ch]->Draw("SAME E1");
    }
    cw->SaveAs((outdir + std::string("/png/") + "waveforms_all_channels.png").c_str());
    cw->SaveAs((outdir + std::string("/pdf/") + "waveforms_all_channels.pdf").c_str());
            { TFile *fr_cw = new TFile((outdir + std::string("/root/waveforms_all_channels.root")).c_str(), "RECREATE"); for (int ch = 0; ch < nch; ch++) { h2_wave[ch]->Write(); p_avg[ch]->Write(); } fr_cw->Close(); }

    // ══════════ 画图：峰位时间 ══════════
    TCanvas *ct1 = new TCanvas("ct1", "Peak Time per Channel", 1400, 800);
    ct1->Divide(4, 2);
    for (int ch = 0; ch < nch; ch++) {
        ct1->cd(ch + 1);
        h_peak_t[ch]->SetLineColor(colors[ch]);
        h_peak_t[ch]->SetLineWidth(2);
        h_peak_t[ch]->Draw();
        double fwhm = GetFWHM(h_peak_t[ch]);
        TPaveText *pt = new TPaveText(0.62, 0.75, 0.88, 0.88, "NDC");
        pt->SetFillColor(0); pt->SetBorderSize(0); pt->SetTextColor(colors[ch]);
        pt->SetTextFont(42);
        pt->AddText(Form("#mu = %.2f ns", h_peak_t[ch]->GetMean()));
        pt->AddText(Form("FWHM = %.2f ns", fwhm));
        pt->Draw();
    }
    ct1->SaveAs((outdir + std::string("/png/") + "peak_time_per_channel.png").c_str());
    ct1->SaveAs((outdir + std::string("/pdf/") + "peak_time_per_channel.pdf").c_str());
    for (int ch = 0; ch < nch; ch++) { ct1->cd(ch + 1); gPad->SetLogy(); }
    ct1->SaveAs((outdir + std::string("/png/") + "peak_time_per_channel_logy.png").c_str());
    ct1->SaveAs((outdir + std::string("/pdf/") + "peak_time_per_channel_logy.pdf").c_str());
            { TFile *fr_ct1 = new TFile((outdir + std::string("/root/peak_time_per_channel.root")).c_str(), "RECREATE"); for (int ch = 0; ch < nch; ch++) h_peak_t[ch]->Write(); fr_ct1->Close(); }

    TCanvas *ct2 = new TCanvas("ct2", "Peak Time - All Channels", 1200, 800);
    TLegend *leg_t = new TLegend(0.62, 0.65, 0.9, 0.9);
    bool drawn_t = false;
    for (int ch = 0; ch < nch; ch++) {
        if (h_peak_t[ch]->GetEntries() == 0) continue;
        h_peak_t[ch]->Draw(drawn_t ? "SAME" : "");
        drawn_t = true;
        double fwhm = GetFWHM(h_peak_t[ch]);
        leg_t->AddEntry(h_peak_t[ch],
                        Form("Ch %d (#mu=%.1f, FWHM=%.1f)", ch, h_peak_t[ch]->GetMean(), fwhm), "l");
    }
    leg_t->Draw();
    ct2->SaveAs((outdir + std::string("/png/") + "peak_time_all_channels.png").c_str());
    ct2->SaveAs((outdir + std::string("/pdf/") + "peak_time_all_channels.pdf").c_str());
    ct2->SetLogy();
    ct2->SaveAs((outdir + std::string("/png/") + "peak_time_all_channels_logy.png").c_str());
    ct2->SaveAs((outdir + std::string("/pdf/") + "peak_time_all_channels_logy.pdf").c_str());
            { TFile *fr_ct2 = new TFile((outdir + std::string("/root/peak_time_all_channels.root")).c_str(), "RECREATE"); for (int ch = 0; ch < nch; ch++) h_peak_t[ch]->Write(); fr_ct2->Close(); }

    // ══════════ 画图：幅度 ══════════
    TCanvas *ca1 = new TCanvas("ca1", "Amplitude per Channel", 1400, 800);
    ca1->Divide(4, 2);
    for (int ch = 0; ch < nch; ch++) {
        ca1->cd(ch + 1);
        h_ampl[ch]->SetLineColor(colors[ch]);
        h_ampl[ch]->SetLineWidth(2);
        h_ampl[ch]->Draw();
    }
    ca1->SaveAs((outdir + std::string("/png/") + "amplitude_per_channel.png").c_str());
    ca1->SaveAs((outdir + std::string("/pdf/") + "amplitude_per_channel.pdf").c_str());
    for (int ch = 0; ch < nch; ch++) { ca1->cd(ch + 1); gPad->SetLogy(); }
    ca1->SaveAs((outdir + std::string("/png/") + "amplitude_per_channel_logy.png").c_str());
    ca1->SaveAs((outdir + std::string("/pdf/") + "amplitude_per_channel_logy.pdf").c_str());
            { TFile *fr_ca1 = new TFile((outdir + std::string("/root/amplitude_per_channel.root")).c_str(), "RECREATE"); for (int ch = 0; ch < nch; ch++) h_ampl[ch]->Write(); fr_ca1->Close(); }

    TCanvas *ca2 = new TCanvas("ca2", "Amplitude - All Channels", 1200, 800);
    TLegend *leg_a = new TLegend(0.75, 0.7, 0.9, 0.9);
    bool drawn_a = false;
    for (int ch = 0; ch < nch; ch++) {
        if (h_ampl[ch]->GetEntries() == 0) continue;
        h_ampl[ch]->Draw(drawn_a ? "SAME" : "");
        drawn_a = true;
        leg_a->AddEntry(h_ampl[ch], Form("Ch %d", ch), "l");
    }
    leg_a->Draw();
    ca2->SaveAs((outdir + std::string("/png/") + "amplitude_all_channels.png").c_str());
    ca2->SaveAs((outdir + std::string("/pdf/") + "amplitude_all_channels.pdf").c_str());
    ca2->SetLogy();
    ca2->SaveAs((outdir + std::string("/png/") + "amplitude_all_channels_logy.png").c_str());
    ca2->SaveAs((outdir + std::string("/pdf/") + "amplitude_all_channels_logy.pdf").c_str());
            { TFile *fr_ca2 = new TFile((outdir + std::string("/root/amplitude_all_channels.root")).c_str(), "RECREATE"); for (int ch = 0; ch < nch; ch++) h_ampl[ch]->Write(); fr_ca2->Close(); }

    // ══════════ 画图：渡越时间 TT (阈值法) ══════════
    TCanvas *ctt1 = new TCanvas("ctt1", "Transit Time (threshold +2mV) per Channel", 1400, 800);
    ctt1->Divide(4, 2);
    for (int ch = 0; ch < nch; ch++) {
        ctt1->cd(ch + 1);
        h_tt_th[ch]->SetLineColor(colors[ch]);
        h_tt_th[ch]->SetLineWidth(2);
        h_tt_th[ch]->Draw();
        double fwhm = GetFWHM(h_tt_th[ch]);
        TPaveText *pt = new TPaveText(0.62, 0.75, 0.88, 0.88, "NDC");
        pt->SetFillColor(0); pt->SetBorderSize(0); pt->SetTextColor(colors[ch]);
        pt->SetTextFont(42);
        pt->AddText(Form("#mu = %.2f ns", h_tt_th[ch]->GetMean()));
        pt->AddText(Form("FWHM = %.2f ns", fwhm));
        pt->Draw();
    }
    ctt1->SaveAs((outdir + std::string("/png/") + "tt_threshold_per_channel.png").c_str());
    ctt1->SaveAs((outdir + std::string("/pdf/") + "tt_threshold_per_channel.pdf").c_str());
    for (int ch = 0; ch < nch; ch++) { ctt1->cd(ch + 1); gPad->SetLogy(); }
    ctt1->SaveAs((outdir + std::string("/png/") + "tt_threshold_per_channel_logy.png").c_str());
    ctt1->SaveAs((outdir + std::string("/pdf/") + "tt_threshold_per_channel_logy.pdf").c_str());
            { TFile *fr_ctt1 = new TFile((outdir + std::string("/root/tt_threshold_per_channel.root")).c_str(), "RECREATE"); for (int ch = 0; ch < nch; ch++) h_tt_th[ch]->Write(); fr_ctt1->Close(); }

    TCanvas *ctt2 = new TCanvas("ctt2", "Transit Time (threshold +2mV) - All Channels", 1200, 800);
    TLegend *leg_tt = new TLegend(0.62, 0.65, 0.9, 0.9);
    bool drawn_th = false;
    for (int ch = 0; ch < nch; ch++) {
        if (h_tt_th[ch]->GetEntries() == 0) continue;
        h_tt_th[ch]->Draw(drawn_th ? "SAME" : "");
        drawn_th = true;
        double fwhm = GetFWHM(h_tt_th[ch]);
        leg_tt->AddEntry(h_tt_th[ch],
                         Form("Ch %d (#mu=%.1f, FWHM=%.1f)", ch, h_tt_th[ch]->GetMean(), fwhm), "l");
    }
    leg_tt->Draw();
    ctt2->SaveAs((outdir + std::string("/png/") + "tt_threshold_all_channels.png").c_str());
    ctt2->SaveAs((outdir + std::string("/pdf/") + "tt_threshold_all_channels.pdf").c_str());
    ctt2->SetLogy();
    ctt2->SaveAs((outdir + std::string("/png/") + "tt_threshold_all_channels_logy.png").c_str());
    ctt2->SaveAs((outdir + std::string("/pdf/") + "tt_threshold_all_channels_logy.pdf").c_str());
            { TFile *fr_ctt2 = new TFile((outdir + std::string("/root/tt_threshold_all_channels.root")).c_str(), "RECREATE"); for (int ch = 0; ch < nch; ch++) h_tt_th[ch]->Write(); fr_ctt2->Close(); }

    // ══════════ 画图：渡越时间 TT (CFD 法) ══════════
    TCanvas *ccfd1 = new TCanvas("ccfd1", "Transit Time (CFD) per Channel", 1400, 800);
    ccfd1->Divide(4, 2);
    for (int ch = 0; ch < nch; ch++) {
        ccfd1->cd(ch + 1);
        h_tt_cfd[ch]->SetLineColor(colors[ch]);
        h_tt_cfd[ch]->SetLineWidth(2);
        h_tt_cfd[ch]->Draw();
        double fwhm = GetFWHM(h_tt_cfd[ch]);
        TPaveText *pt = new TPaveText(0.62, 0.75, 0.88, 0.88, "NDC");
        pt->SetFillColor(0); pt->SetBorderSize(0); pt->SetTextColor(colors[ch]);
        pt->SetTextFont(42);
        pt->AddText(Form("#mu = %.2f ns", h_tt_cfd[ch]->GetMean()));
        pt->AddText(Form("FWHM = %.2f ns", fwhm));
        pt->Draw();
    }
    ccfd1->SaveAs((outdir + std::string("/png/") + "tt_cfd_per_channel.png").c_str());
    ccfd1->SaveAs((outdir + std::string("/pdf/") + "tt_cfd_per_channel.pdf").c_str());
    for (int ch = 0; ch < nch; ch++) { ccfd1->cd(ch + 1); gPad->SetLogy(); }
    ccfd1->SaveAs((outdir + std::string("/png/") + "tt_cfd_per_channel_logy.png").c_str());
    ccfd1->SaveAs((outdir + std::string("/pdf/") + "tt_cfd_per_channel_logy.pdf").c_str());
            { TFile *fr_ccfd1 = new TFile((outdir + std::string("/root/tt_cfd_per_channel.root")).c_str(), "RECREATE"); for (int ch = 0; ch < nch; ch++) h_tt_cfd[ch]->Write(); fr_ccfd1->Close(); }

    TCanvas *ccfd2 = new TCanvas("ccfd2", "Transit Time (CFD) - All Channels", 1200, 800);
    TLegend *leg_cfd = new TLegend(0.62, 0.65, 0.9, 0.9);
    bool drawn_cfd = false;
    for (int ch = 0; ch < nch; ch++) {
        if (h_tt_cfd[ch]->GetEntries() == 0) continue;
        h_tt_cfd[ch]->Draw(drawn_cfd ? "SAME" : "");
        drawn_cfd = true;
        double fwhm = GetFWHM(h_tt_cfd[ch]);
        leg_cfd->AddEntry(h_tt_cfd[ch],
                          Form("Ch %d (#mu=%.1f, FWHM=%.1f)", ch, h_tt_cfd[ch]->GetMean(), fwhm), "l");
    }
    leg_cfd->Draw();
    ccfd2->SaveAs((outdir + std::string("/png/") + "tt_cfd_all_channels.png").c_str());
    ccfd2->SaveAs((outdir + std::string("/pdf/") + "tt_cfd_all_channels.pdf").c_str());
    ccfd2->SetLogy();
    ccfd2->SaveAs((outdir + std::string("/png/") + "tt_cfd_all_channels_logy.png").c_str());
    ccfd2->SaveAs((outdir + std::string("/pdf/") + "tt_cfd_all_channels_logy.pdf").c_str());
            { TFile *fr_ccfd2 = new TFile((outdir + std::string("/root/tt_cfd_all_channels.root")).c_str(), "RECREATE"); for (int ch = 0; ch < nch; ch++) h_tt_cfd[ch]->Write(); fr_ccfd2->Close(); }

    // ══════════ 画图：时间演化 ══════════
    if (enable_time_evolution) {
        // 电荷 vs 时间 — 每通道分开
        TCanvas *cqt1 = new TCanvas("cqt1", "Charge vs Time per Channel", 1400, 800);
        cqt1->Divide(4, 2);
        for (int ch = 0; ch < nch; ch++) {
            cqt1->cd(ch + 1);
            p_q_vs_t[ch]->SetLineColor(colors[ch]);
            p_q_vs_t[ch]->SetMarkerColor(kBlack);
            p_q_vs_t[ch]->SetMarkerStyle(20);
            p_q_vs_t[ch]->SetMarkerSize(0.3);
            p_q_vs_t[ch]->Draw();
        }
        cqt1->SaveAs((outdir + std::string("/png/") + "charge_vs_time_per_channel.png").c_str());
        cqt1->SaveAs((outdir + std::string("/pdf/") + "charge_vs_time_per_channel.pdf").c_str());
            { TFile *fr_cqt1 = new TFile((outdir + std::string("/root/charge_vs_time_per_channel.root")).c_str(), "RECREATE"); for (int ch = 0; ch < nch; ch++) p_q_vs_t[ch]->Write(); fr_cqt1->Close(); }

        // 电荷 vs 时间 — 所有通道叠加
        TCanvas *cqt2 = new TCanvas("cqt2", "Charge vs Time - All Channels", 1400, 600);
        double qlo, qhi;
        GetProfileRange(p_q_vs_t, nch, qlo, qhi);
        cqt2->DrawFrame(time_xmin, qlo, time_xmax, qhi,
                        "Charge vs Time;Time [s];Charge [pC]");
        TLegend *leg_qt = new TLegend(0.82, 0.15, 0.92, 0.4);
        for (int ch = 0; ch < nch; ch++) {
            if (p_q_vs_t[ch]->GetEntries() == 0) continue;
            p_q_vs_t[ch]->SetLineColor(colors[ch]);
            p_q_vs_t[ch]->SetMarkerColor(kBlack);
            p_q_vs_t[ch]->SetMarkerStyle(20);
            p_q_vs_t[ch]->SetMarkerSize(0.2);
            p_q_vs_t[ch]->Draw("E1 SAME");
            leg_qt->AddEntry(p_q_vs_t[ch], Form("Ch %d", ch), "lp");
        }
        leg_qt->Draw();
        cqt2->SaveAs((outdir + std::string("/png/") + "charge_vs_time_all_channels.png").c_str());
        cqt2->SaveAs((outdir + std::string("/pdf/") + "charge_vs_time_all_channels.pdf").c_str());
            { TFile *fr_cqt2 = new TFile((outdir + std::string("/root/charge_vs_time_all_channels.root")).c_str(), "RECREATE"); for (int ch = 0; ch < nch; ch++) p_q_vs_t[ch]->Write(); fr_cqt2->Close(); }

        // 基线 vs 时间 — 每通道（纵轴覆盖实际时间箱均值）
        TCanvas *cbt1 = new TCanvas("cbt1", "Baseline vs Time per Channel", 1400, 800);
        cbt1->Divide(4, 2);
        for (int ch = 0; ch < nch; ch++) {
            cbt1->cd(ch + 1);
            double bl_ymin, bl_ymax;
            GetProfileRange(&p_bl_vs_t[ch], 1, bl_ymin, bl_ymax);
            gPad->DrawFrame(time_xmin, bl_ymin, time_xmax, bl_ymax,
                            Form("Ch %d Baseline vs Time;Time [s];Baseline [ADC]", ch));
            p_bl_vs_t[ch]->SetLineColor(colors[ch]);
            p_bl_vs_t[ch]->SetMarkerColor(kBlack);
            p_bl_vs_t[ch]->SetMarkerStyle(20);
            p_bl_vs_t[ch]->SetMarkerSize(0.3);
            p_bl_vs_t[ch]->Draw("E1 SAME");
        }
        cbt1->SaveAs((outdir + std::string("/png/") + "baseline_vs_time_per_channel.png").c_str());
        cbt1->SaveAs((outdir + std::string("/pdf/") + "baseline_vs_time_per_channel.pdf").c_str());
            { TFile *fr_cbt1 = new TFile((outdir + std::string("/root/baseline_vs_time_per_channel.root")).c_str(), "RECREATE"); for (int ch = 0; ch < nch; ch++) p_bl_vs_t[ch]->Write(); fr_cbt1->Close(); }

        // 基线 vs 时间 — 叠加
        TCanvas *cbt2 = new TCanvas("cbt2", "Baseline vs Time - All Channels", 1400, 600);
        double blo, bhi;
        GetProfileRange(p_bl_vs_t, nch, blo, bhi);
        cbt2->DrawFrame(time_xmin, blo, time_xmax, bhi,
                        "Baseline vs Time;Time [s];Baseline [ADC]");
        TLegend *leg_bt = new TLegend(0.82, 0.15, 0.92, 0.4);
        for (int ch = 0; ch < nch; ch++) {
            if (p_bl_vs_t[ch]->GetEntries() == 0) continue;
            p_bl_vs_t[ch]->SetLineColor(colors[ch]);
            p_bl_vs_t[ch]->SetMarkerColor(kBlack);
            p_bl_vs_t[ch]->SetMarkerStyle(20);
            p_bl_vs_t[ch]->SetMarkerSize(0.2);
            p_bl_vs_t[ch]->Draw("E1 SAME");
            leg_bt->AddEntry(p_bl_vs_t[ch], Form("Ch %d", ch), "lp");
        }
        leg_bt->Draw();
        cbt2->SaveAs((outdir + std::string("/png/") + "baseline_vs_time_all_channels.png").c_str());
        cbt2->SaveAs((outdir + std::string("/pdf/") + "baseline_vs_time_all_channels.pdf").c_str());
            { TFile *fr_cbt2 = new TFile((outdir + std::string("/root/baseline_vs_time_all_channels.root")).c_str(), "RECREATE"); for (int ch = 0; ch < nch; ch++) p_bl_vs_t[ch]->Write(); fr_cbt2->Close(); }

        // 幅度 vs 时间 — 每通道
        TCanvas *cat1 = new TCanvas("cat1", "Amplitude vs Time per Channel", 1400, 800);
        cat1->Divide(4, 2);
        for (int ch = 0; ch < nch; ch++) {
            cat1->cd(ch + 1);
            p_ampl_vs_t[ch]->SetLineColor(colors[ch]);
            p_ampl_vs_t[ch]->SetMarkerColor(kBlack);
            p_ampl_vs_t[ch]->SetMarkerStyle(20);
            p_ampl_vs_t[ch]->SetMarkerSize(0.3);
            p_ampl_vs_t[ch]->Draw();
        }
        cat1->SaveAs((outdir + std::string("/png/") + "amplitude_vs_time_per_channel.png").c_str());
        cat1->SaveAs((outdir + std::string("/pdf/") + "amplitude_vs_time_per_channel.pdf").c_str());
            { TFile *fr_cat1 = new TFile((outdir + std::string("/root/amplitude_vs_time_per_channel.root")).c_str(), "RECREATE"); for (int ch = 0; ch < nch; ch++) p_ampl_vs_t[ch]->Write(); fr_cat1->Close(); }

        // 幅度 vs 时间 — 叠加
        TCanvas *cat2 = new TCanvas("cat2", "Amplitude vs Time - All Channels", 1400, 600);
        double alo, ahi;
        GetProfileRange(p_ampl_vs_t, nch, alo, ahi);
        cat2->DrawFrame(time_xmin, alo, time_xmax, ahi,
                        "Amplitude vs Time;Time [s];Amplitude [mV]");
        TLegend *leg_at = new TLegend(0.82, 0.15, 0.92, 0.4);
        for (int ch = 0; ch < nch; ch++) {
            if (p_ampl_vs_t[ch]->GetEntries() == 0) continue;
            p_ampl_vs_t[ch]->SetLineColor(colors[ch]);
            p_ampl_vs_t[ch]->SetMarkerColor(kBlack);
            p_ampl_vs_t[ch]->SetMarkerStyle(20);
            p_ampl_vs_t[ch]->SetMarkerSize(0.2);
            p_ampl_vs_t[ch]->Draw("E1 SAME");
            leg_at->AddEntry(p_ampl_vs_t[ch], Form("Ch %d", ch), "lp");
        }
        leg_at->Draw();
        cat2->SaveAs((outdir + std::string("/png/") + "amplitude_vs_time_all_channels.png").c_str());
        cat2->SaveAs((outdir + std::string("/pdf/") + "amplitude_vs_time_all_channels.pdf").c_str());
            { TFile *fr_cat2 = new TFile((outdir + std::string("/root/amplitude_vs_time_all_channels.root")).c_str(), "RECREATE"); for (int ch = 0; ch < nch; ch++) p_ampl_vs_t[ch]->Write(); fr_cat2->Close(); }

        // 计数率 vs 时间 — 每通道
        TCanvas *crt1 = new TCanvas("crt1", "Count Rate vs Time per Channel", 1400, 800);
        crt1->Divide(4, 2);
        double bin_width = (time_xmax - time_xmin) / time_bins;
        for (int ch = 0; ch < nch; ch++) {
            crt1->cd(ch + 1);
            double raw_rate_entries = h_rate_vs_t[ch]->GetEntries();
            for (int ib = 1; ib <= h_rate_vs_t[ch]->GetNbinsX(); ib++) {
                double lo = h_rate_vs_t[ch]->GetBinLowEdge(ib);
                double exposure = std::max(0., std::min(t_range, lo + bin_width) - lo);
                double count = h_rate_vs_t[ch]->GetBinContent(ib);
                h_rate_vs_t[ch]->SetBinContent(ib, exposure > 0 ? count / exposure : 0);
                h_rate_vs_t[ch]->SetBinError(ib, exposure > 0 ? std::sqrt(count) / exposure : 0);
            }
            h_rate_vs_t[ch]->SetEntries(raw_rate_entries);
            h_rate_vs_t[ch]->SetLineColor(colors[ch]);
            h_rate_vs_t[ch]->SetLineWidth(2);
            h_rate_vs_t[ch]->Draw();
        }
        crt1->SaveAs((outdir + std::string("/png/") + "count_rate_vs_time_per_channel.png").c_str());
        crt1->SaveAs((outdir + std::string("/pdf/") + "count_rate_vs_time_per_channel.pdf").c_str());
        for (int ch = 0; ch < nch; ch++) { crt1->cd(ch + 1); gPad->SetLogy(); }
        crt1->SaveAs((outdir + std::string("/png/") + "count_rate_vs_time_per_channel_logy.png").c_str());
        crt1->SaveAs((outdir + std::string("/pdf/") + "count_rate_vs_time_per_channel_logy.pdf").c_str());
            { TFile *fr_crt1 = new TFile((outdir + std::string("/root/count_rate_vs_time_per_channel.root")).c_str(), "RECREATE"); for (int ch = 0; ch < nch; ch++) h_rate_vs_t[ch]->Write(); fr_crt1->Close(); }

        // 计数率 vs 时间 — 叠加
        TCanvas *crt2 = new TCanvas("crt2", "Count Rate vs Time - All Channels", 1400, 600);
        TLegend *leg_rt = new TLegend(0.82, 0.15, 0.92, 0.4);
        bool drawn_rate = false;
        for (int ch = 0; ch < nch; ch++) {
            if (h_rate_vs_t[ch]->GetEntries() == 0) continue;
            h_rate_vs_t[ch]->SetLineColor(colors[ch]);
            h_rate_vs_t[ch]->SetLineWidth(2);
            h_rate_vs_t[ch]->Draw(drawn_rate ? "SAME" : "");
            drawn_rate = true;
            leg_rt->AddEntry(h_rate_vs_t[ch], Form("Ch %d", ch), "l");
        }
        leg_rt->Draw();
        crt2->SaveAs((outdir + std::string("/png/") + "count_rate_vs_time_all_channels.png").c_str());
        crt2->SaveAs((outdir + std::string("/pdf/") + "count_rate_vs_time_all_channels.pdf").c_str());
        crt2->SetLogy();
        crt2->SaveAs((outdir + std::string("/png/") + "count_rate_vs_time_all_channels_logy.png").c_str());
        crt2->SaveAs((outdir + std::string("/pdf/") + "count_rate_vs_time_all_channels_logy.pdf").c_str());
            { TFile *fr_crt2 = new TFile((outdir + std::string("/root/count_rate_vs_time_all_channels.root")).c_str(), "RECREATE"); for (int ch = 0; ch < nch; ch++) h_rate_vs_t[ch]->Write(); fr_crt2->Close(); }

        cout << "\nSaved time-evolution plots" << endl;
    }

    // ══════════ 终端统计 ══════════
    cout << "\n====== Charge Summary ======" << endl;
    cout << "Adaptive baseline / signal [start,end), shared online kernel" << endl;
    cout << "Raw waveforms: " << raw_wave_count << "; quality rejected: " << rejected_wave_count << endl;
    cout << "Additional amplitude cut rejected: " << amplitude_rejected_wave_count << endl;
    cout << "Time axis: relative to first saved trigger; NOT UTC. Timing: waveform-local, uncalibrated." << endl;
    cout << "Unit: pC  (1 ADC·sample = 0.5mV × 2ns ÷ 50Ω = 0.02 pC)" << endl;
    cout << "Ch   Entries     Q_mean[pC]   Q_RMS" << endl;
    for (int ch = 0; ch < nch; ch++)
        cout << Form(" %d  %8.0f  %12.1f  %10.1f",
                     ch, h_q[ch]->GetEntries(), h_q[ch]->GetMean(), h_q[ch]->GetRMS()) << endl;

    cout << "\n====== Waveform Summary ======" << endl;
    cout << "Ch   Waveforms   Peak_t[ns]  FWHM[ns]  Ampl_mean[mV]   Ampl_RMS[mV]" << endl;
    for (int ch = 0; ch < nch; ch++)
        cout << Form(" %d  %8d  %10.2f  %8.2f  %13.2f  %13.2f",
                     ch, wf_count[ch], h_peak_t[ch]->GetMean(),
                     GetFWHM(h_peak_t[ch]),
                     h_ampl[ch]->GetMean(), h_ampl[ch]->GetRMS()) << endl;

    cout << "\n====== Transit Time Summary ======" << endl;
    cout << "Method 1: threshold = baseline + " << tt_threshold_mv << " mV" << endl;
    cout << "Method 2: CFD fraction = " << tt_cfd_fraction * 100 << "% of peak" << endl;
    cout << "Ch   Entries   TT_thr_mean[ns]  FWHM_th  TT_cfd_mean[ns]  FWHM_cfd" << endl;
    for (int ch = 0; ch < nch; ch++)
        cout << Form(" %d  %8.0f  %14.2f  %8.2f  %14.2f  %8.2f",
                     ch, h_tt_th[ch]->GetEntries(),
                     h_tt_th[ch]->GetMean(), GetFWHM(h_tt_th[ch]),
                     h_tt_cfd[ch]->GetMean(), GetFWHM(h_tt_cfd[ch])) << endl;

    // ══════════ 保存所有直方图 ══════════
    TFile *fout = new TFile((outdir + std::string("/root/analyze_charge.root")).c_str(), "RECREATE");
    for (int ch = 0; ch < nch; ch++) {
        h_q[ch]->Write();
        if (enable_tail_correction) h_q_tail[ch]->Write();
        h2_wave[ch]->Write();
        p_avg[ch]->Write();
        h_peak_t[ch]->Write();
        h_ampl[ch]->Write();
        h_tt_th[ch]->Write();
        h_tt_cfd[ch]->Write();
        if (enable_time_evolution) {
            p_q_vs_t[ch]->Write();
            p_bl_vs_t[ch]->Write();
            p_ampl_vs_t[ch]->Write();
            h_rate_vs_t[ch]->Write();
        }
    }
    fout->Close();
    cout << "\nSaved histograms to " << outdir << "/root/analyze_charge.root" << endl;

    cout << "\n=== Output images in " << outdir << "/ ===" << endl;
    cout << "  png/charge_all_channels.png, pdf/charge_all_channels.pdf, root/charge_all_channels.root" << endl;
    cout << "  charge_all_channels_logy.png / .pdf" << endl;
    cout << "  charge_per_channel.png / .pdf" << endl;
    cout << "  charge_per_channel_logy.png / .pdf" << endl;
    if (enable_tail_correction)
        cout << "  charge_comparison.png / .pdf / _logy" << endl;
    cout << "  waveforms_all_channels.png / .pdf" << endl;
    cout << "  peak_time_per_channel.png / .pdf" << endl;
    cout << "  peak_time_per_channel_logy.png / .pdf" << endl;
    cout << "  peak_time_all_channels.png / .pdf" << endl;
    cout << "  peak_time_all_channels_logy.png / .pdf" << endl;
    cout << "  amplitude_per_channel.png / .pdf" << endl;
    cout << "  amplitude_per_channel_logy.png / .pdf" << endl;
    cout << "  amplitude_all_channels.png / .pdf" << endl;
    cout << "  amplitude_all_channels_logy.png / .pdf" << endl;
    cout << "  tt_threshold_per_channel.png / .pdf / _logy" << endl;
    cout << "  tt_threshold_all_channels.png / .pdf / _logy" << endl;
    cout << "  tt_cfd_per_channel.png / .pdf / _logy" << endl;
    cout << "  tt_cfd_all_channels.png / .pdf / _logy" << endl;
    if (enable_time_evolution) {
        cout << "  charge_vs_time_per_channel.png / .pdf" << endl;
        cout << "  charge_vs_time_all_channels.png / .pdf" << endl;
        cout << "  baseline_vs_time_per_channel.png / .pdf" << endl;
        cout << "  baseline_vs_time_all_channels.png / .pdf" << endl;
        cout << "  amplitude_vs_time_per_channel.png / .pdf" << endl;
        cout << "  amplitude_vs_time_all_channels.png / .pdf" << endl;
        cout << "  count_rate_vs_time_per_channel.png / .pdf / _logy" << endl;
        cout << "  count_rate_vs_time_all_channels.png / .pdf / _logy" << endl;
    }
}
