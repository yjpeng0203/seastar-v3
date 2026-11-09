"""Post-test archive jobs controlled by the waveform-monitor webpage."""
from dataclasses import replace
import importlib.util
import json
import multiprocessing as mp
import os
from pathlib import Path
import queue
import shutil
import signal
import sys
import threading
import time
import warnings

PROJECT=Path(__file__).resolve().parents[2]
if str(PROJECT) not in sys.path:sys.path.insert(0,str(PROJECT))
from organizer.config import OrganizerConfig,load_organizer_config
from organizer.discovery import discover_runs,discover_trigger_signals


def archive_worker(settings,selection,session_name,events):
    from organizer.packager import finalize_session,verify_dataset
    from organizer.state import StateStore,decode_lock
    os.setsid();os.nice(5);cfg=OrganizerConfig(**settings);state=None;result=None
    try:
        with decode_lock(cfg.resolve_work_dir()):
            runs=[r for r in discover_runs(cfg.resolve_raw_dir(),cfg,finalize=True) if r.key in selection]
            if {r.key for r in runs}!=set(selection):raise RuntimeError('所选 Run 在任务启动前发生变化，请刷新列表后重试')
            before={str(v.path):(v.path.stat().st_size,v.path.stat().st_mtime_ns,v.path.stat().st_mode) for r in runs for v in r.volumes}
            for run in runs:
                for path in discover_trigger_signals(cfg.resolve_raw_dir(),run.board_id,run.run_str):
                    s=path.stat();before[str(path)]=(s.st_size,s.st_mtime_ns,s.st_mode)
            def unchanged():
                after_runs=[r for r in discover_runs(cfg.resolve_raw_dir(),cfg,finalize=True) if r.key in selection]
                paths={str(v.path) for r in after_runs for v in r.volumes}
                paths.update(str(p) for r in after_runs for p in discover_trigger_signals(cfg.resolve_raw_dir(),r.board_id,r.run_str))
                if paths!=set(before):raise RuntimeError('所选 Run 的文件清单发生变化，请确认本轮停采后重试')
                for path,signature in before.items():
                    s=Path(path).stat()
                    if (s.st_size,s.st_mtime_ns,s.st_mode)!=signature:raise RuntimeError('所选原始文件在归档期间发生变化，请确认本轮停采后重试：'+Path(path).name)
            state=StateStore(cfg.resolve_work_dir()/'state.sqlite3')
            result=finalize_session(cfg,state,session_name=session_name,run_filter=selection,
                on_progress=lambda p:events.put(dict(type='progress',progress=p)))
            unchanged()
            if result.get('session_dir'):
                events.put(dict(type='progress',progress=dict(stage='verify',message='核验批次结构、ROOT 计数和 SHA-256')))
                verification=verify_dataset(Path(result['session_dir']),root_bin=cfg.root_bin,board_map=cfg.board_map,work_dir=cfg.resolve_work_dir())
                if not verification['ok']:raise RuntimeError('归档校验失败：'+'；'.join(verification['errors']))
                result['verification']=dict(ok=True,warnings=verification.get('warnings',[]))
            unchanged()
            events.put(dict(type='result',status='complete',result=result,error=None,progress=dict(stage='complete',message='归档完成')))
    except Exception as error:
        if result:
            result['status']='generated_unverified'
            result['verification']=dict(ok=False,warnings=['文件已生成，但最终校验未通过；请保留文件，修正后重试校验。'])
        events.put(dict(type='result',status='error',error=str(error),result=result,progress=dict(stage='error',message='文件已生成，最终校验未通过' if result else '归档未完成；已验证的分卷检查点保留')))
    finally:
        if state:state.close()


class ArchiveService:
    def __init__(self,data_dir,state_dir,config_path=None,enabled=True):
        self.data_dir=Path(data_dir).resolve();self.state_dir=Path(state_dir).resolve();self.enabled=enabled
        self.file=self.state_dir/'archive-settings.json';self.result_file=self.state_dir/'last-archive.json'
        self.guard=threading.RLock();self.ctx=mp.get_context('spawn');self.events=self.ctx.Queue();self.process=None;self.closed=False
        problem=None;self.config=OrganizerConfig(raw_data_dir=str(self.data_dir),backend='ipc',work_dir=str(self.state_dir/'archive-work'),output_root=str(self.data_dir.parent/'organized'))
        if config_path:
            try:self.config=load_organizer_config(config_path,dict(raw_data_dir=str(self.data_dir),backend='ipc'))
            except (OSError,ValueError,TypeError) as error:problem='读取归档配置失败：'+str(error)
        csv_dir=self.config.csv_mirror_dir
        fee_config=PROJECT/'fee-monitor/config.json'
        default_csv=Path(OrganizerConfig().csv_mirror_dir).expanduser().resolve()
        if fee_config.is_file() and (not config_path or Path(csv_dir).expanduser().resolve()==default_csv or not Path(csv_dir).expanduser().exists()):
            try:csv_dir=json.loads(fee_config.read_text()).get('local_dir',csv_dir)
            except (OSError,ValueError):pass
        self.settings=dict(output_dir=str(self.config.resolve_output_root()),csv_dir=csv_dir,session_name='',decoder_bin=self.config.decoder_bin,decoder_config=self.config.decoder_config)
        if self.file.is_file():
            try:self.set_settings(json.loads(self.file.read_text()))
            except (OSError,ValueError,TypeError) as error:problem='读取页面归档设置失败：'+str(error)
        self.value=dict(status=('error' if problem else 'idle') if enabled else 'disabled',enabled=enabled,error=problem,notice='',available=False,paths={},runs=[],progress={},result=None)
        if self.result_file.is_file():
            try:self.value['result']=json.loads(self.result_file.read_text())
            except (OSError,ValueError):pass
        self.thread=threading.Thread(target=self.collect,daemon=True);self.thread.start()

    def set_settings(self,values):
        if not isinstance(values,dict) or set(values)-set(self.settings):raise ValueError('归档设置字段无效')
        for key,value in values.items():
            if not isinstance(value,str) or (not value.strip() and key!='session_name') or len(value)>4096 or '\x00' in value:raise ValueError('请输入有效的归档路径')
        name=values.get('session_name',self.settings['session_name']).strip()
        if name in ('.','..') or '/' in name or '\\' in name:raise ValueError('批次名称不能含目录分隔符')
        self.settings.update({k:v.strip() for k,v in values.items()})

    def configuration(self):
        return replace(self.config,output_root=self.settings['output_dir'],csv_mirror_dir=self.settings['csv_dir'],
            decoder_bin=self.settings['decoder_bin'],decoder_config=self.settings['decoder_config'],link_mode='copy',readonly_protect=True,layout='group')

    def inventory(self,cfg):
        result=[]
        for r in sorted(discover_runs(self.data_dir,cfg,finalize=True),key=lambda r:(r.start_stamp,r.run_id,r.board_id),reverse=True):
            signals=discover_trigger_signals(self.data_dir,r.board_id,r.run_str)
            result.append(dict(key=r.key,board_id=r.board_id,string_group=r.string_group,run_id=r.run_str,volumes=len(r.volumes),
                sealed_volumes=sum(v.sealed for v in r.volumes),missing_indices=r.missing_indices,
                bytes=r.total_bytes+sum(p.stat().st_size for p in signals),trigger_files=len(signals),start_stamp=r.start_stamp))
        return result

    def prepare(self):
        from organizer.decoder_runner import select_backend
        cfg=self.configuration();out=cfg.resolve_output_root()
        if out==self.data_dir or self.data_dir in out.parents:raise ValueError('归档保存目录请设在原始 DAT 目录之外')
        with warnings.catch_warnings(record=True) as notices:
            warnings.simplefilter('always');select_backend(cfg);decoder_config=cfg.resolve_decoder_config()
        if not os.access(str(cfg.resolve_decoder_bin()),os.X_OK):raise ValueError('decoder 程序没有执行权限')
        if not (shutil.which(cfg.hadd_bin) or Path(cfg.hadd_bin).is_file()):raise ValueError('未找到 hadd，无法完成 ROOT 合并')
        if not (shutil.which(cfg.root_bin) or Path(cfg.root_bin).is_file() or importlib.util.find_spec('ROOT')):raise ValueError('ROOT 校验需要 root 命令或 PyROOT')
        self.value.update(available=True,error=None,notice='；'.join(dict.fromkeys(str(w.message) for w in notices)),
            paths=dict(decoder_bin=str(cfg.resolve_decoder_bin()),decoder_config=str(decoder_config),work_dir=str(cfg.resolve_work_dir()),output_dir=str(out),csv_dir=str(cfg.resolve_csv_dir())))
        return cfg

    def collect(self):
        while not self.closed:
            try:event=self.events.get(timeout=.2)
            except queue.Empty:
                with self.guard:
                    if self.process and not self.process.is_alive() and self.value['status']=='running':self.value.update(status='error',error='归档进程已退出，分卷检查点保留')
                continue
            with self.guard:
                kind=event.pop('type');self.value.update(event)
                if kind=='result':self.value['finished_at']=time.time()
                if kind=='result' and event.get('result') and event.get('status')=='complete':
                    try:
                        self.state_dir.mkdir(parents=True,exist_ok=True);tmp=self.result_file.with_suffix('.tmp');tmp.write_text(json.dumps(event['result'],ensure_ascii=False,indent=2));tmp.replace(self.result_file)
                    except OSError as error:self.value['notice']='归档已完成，但保存页面任务记录失败：'+str(error)

    def snapshot(self):
        with self.guard:
            if self.enabled and self.value['status']!='running':self.value['runs']=self.inventory(self.configuration())
            elapsed=max(0,(self.value.get('finished_at') or time.time())-self.value.get('started_at',time.time()))
            return dict(self.value,settings=dict(self.settings),raw_data_dir=str(self.data_dir),elapsed_s=elapsed)

    def control(self,command):
        if not isinstance(command,dict) or command.get('action') not in ('check','start'):raise ValueError('归档操作无效')
        with self.guard:
            if not self.enabled:raise ValueError('请在本机 DAT 目录监控模式使用测试结束归档')
            if self.process and self.value['status'] in ('complete','error'):self.process.join(timeout=1)
            if self.process and self.process.is_alive():raise ValueError('归档正在进行，请等待本批次完成')
            if 'settings' in command:self.set_settings(command['settings'])
            try:cfg=self.prepare()
            except (OSError,ValueError,RuntimeError) as error:
                self.value.update(status='error',available=False,error=str(error));return self.snapshot()
            if command['action']=='start':
                if command.get('confirmed_stopped') is not True:raise ValueError('请确认本轮测试已结束，所选数据不再写入')
                selection=command.get('runs');boards=command.get('boards',[])
                if not isinstance(selection,list) or not selection or not all(isinstance(s,str) for s in selection):raise ValueError('请选择本轮需要归档的 Run')
                if not isinstance(boards,list) or any(type(b) is not int or not 0<=b<=255 for b in boards):raise ValueError('归档串筛选无效')
                runs=discover_runs(self.data_dir,cfg,finalize=True);chosen=[r for r in runs if r.key in selection and (not boards or r.board_id in boards)]
                if {r.key for r in chosen}!=set(selection):raise ValueError('所选 Run 已变化或串筛选不匹配，请刷新列表')
                if any(r.missing_indices for r in chosen):raise ValueError('所选 Run 缺少中间分卷，请补齐后归档')
                if any(not r.all_sealed for r in chosen):raise ValueError('所选末卷尚未静默，请确认停采并稍后重试')
                name=self.settings['session_name']
                if name:
                    destination=(cfg.resolve_output_root()/name).resolve()
                    if cfg.resolve_output_root() not in destination.parents:raise ValueError('批次目录必须位于归档保存目录中')
                    manifest=destination/'manifest.json'
                    if manifest.is_file():
                        existing=json.loads(manifest.read_text())
                        if {r['run_key'] for r in existing.get('runs',[])}!=set(selection):raise ValueError('该批次名称已用于另一组 Run，请更换名称')
            self.state_dir.mkdir(parents=True,exist_ok=True);tmp=self.file.with_suffix('.tmp');tmp.write_text(json.dumps(self.settings,ensure_ascii=False,indent=2));tmp.replace(self.file)
            if command['action']=='check':self.value.update(status='idle');return self.snapshot()
            if self.process:self.process.join(timeout=1)
            self.value.update(status='running',error=None,result=None,started_at=time.time(),finished_at=None,progress=dict(stage='prepare',message='准备归档所选 Run'))
            self.process=self.ctx.Process(target=archive_worker,args=(cfg.to_dict(),selection,self.settings['session_name'] or None,self.events),name='SeaStar-archive');self.process.start()
            return self.snapshot()

    def close(self):
        if self.process:
            self.process.join(timeout=30)
            if self.process.is_alive():
                try:os.killpg(self.process.pid,signal.SIGTERM)
                except ProcessLookupError:pass
                self.process.join(timeout=5)
        self.closed=True;self.thread.join(timeout=2)
