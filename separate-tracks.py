"""Persistent source separator, run on a configured CPU/GPU host, not inside the web page.
Requires FFmpeg, espeak-ng and Demucs. No proprietary Moises models or samples.
"""
import os, json, pathlib, tempfile, time, threading, subprocess, math, wave, array, zipfile, urllib.request, urllib.parse, sys
BASE=os.environ.get('PLATFORM_URL','').rstrip('/')
KEY=os.environ.get('TRACK_PROCESSOR_KEY','')
PART=8*1024**2
RATE=44100
USER_AGENT='Plataforma-Adoracao-Processor/1.0'
NAMES={'vocals':'01 Voz Principal','drums':'02 Bateria','bass':'03 Baixo','guitar':'04 Guitarra Violao','piano':'05 Teclas Piano','other':'06 Outros Instrumentos','click':'07 Click','guide':'08 Guia'}
def call(action,job=None,data=None,**params):
    params={'action':action,**params}
    if job:
        params.update(id=job['id'],lease=job['lease'])
        if job.get('wake'): params['wake']=job['wake']
    raw=isinstance(data,bytes)
    request=urllib.request.Request(BASE+'/api/separation-worker?'+urllib.parse.urlencode(params),data=data if raw else (json.dumps(data).encode() if data is not None else None),headers={'Authorization':'Bearer '+KEY,'User-Agent':USER_AGENT,'Content-Type':'application/octet-stream' if raw else 'application/json'})
    with urllib.request.urlopen(request,timeout=120) as r:return json.load(r)
def command(args,timeout=3600):
    # Arguments stay separate; uploaded filenames never reach a shell.
    subprocess.run(args,check=True,timeout=timeout,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
def separation_settings(config):
    refined=config.get('quality','refined')!='fast'
    model='htdemucs_6s' if config['model']=='6' else ('htdemucs_ft' if refined else 'htdemucs')
    return model, ['--shifts','2' if refined else '0','--overlap','0.5' if refined else '0.25','--segment','7']

def presence_from_energy(energies):
    total=max(1e-20,sum(v['energy'] for v in energies.values()))
    result={}
    for family,values in energies.items():
        share=values['energy']/total
        rms_db=10*math.log10(max(values['energy'],1e-20))
        peak_db=20*math.log10(max(values['peak'],1e-10))
        # Conservative activity estimate, never destructive gating or removal.
        present=rms_db>-70 and share>=.0001 and values['activeFraction']>=.005
        result[family]={'present':present,'rmsDb':round(rms_db,2),'peakDb':round(peak_db,2),'energyShare':round(share,6),'activeFraction':round(values['activeFraction'],6)}
    return result

def analyze_stems(root,stems):
    import numpy as np
    energies={}
    for family,stem in stems.items():
        raw=root/(family+'-analysis.f32')
        command(['ffmpeg','-y','-i',str(stem),'-ar',str(RATE),'-ac','2','-f','f32le',str(raw)])
        samples=np.memmap(raw,dtype='<f4',mode='r')
        energy=0.;peak=0.;active=0;blocks=0
        for start in range(0,len(samples),RATE//5):
            block=samples[start:start+RATE//5].astype(np.float64)
            power=float(np.mean(block*block));energy+=power*len(block);peak=max(peak,float(np.max(np.abs(block))));active+=power>10**(-60/10);blocks+=1
        energies[family]={'energy':energy/max(1,len(samples)),'peak':peak,'activeFraction':active/max(1,blocks)}
        del samples;raw.unlink()
    return presence_from_energy(energies)

def grid(config,duration):
    beats=2 if config['meter']=='6/8' else int(config['meter'].split('/')[0])
    beat=60/config['bpm']; prefix=beats*beat
    total=round((duration+prefix)*RATE)
    count=[round(i*beat*RATE) for i in range(beats)]
    music=[]; t=prefix+config['firstBeat']
    while round(t*RATE)<total:
        music.append(round(t*RATE));t+=beat
    return prefix,total,count+music

def cues(root,config,duration):
    prefix,total,times=grid(config,duration)
    click=array.array('h',[0])*(total*2)
    for start in times:
        for i in range(round(.035*RATE)):
            at=start+i
            if at>=total:break
            value=round(8500*math.sin(2*math.pi*1400*i/RATE)*math.exp(-i/(.006*RATE)))
            click[at*2]=value;click[at*2+1]=value
    if sys.byteorder!='little':click.byteswap()
    with wave.open(str(root/'Click.wav'),'wb') as w:
        w.setnchannels(2);w.setsampwidth(2);w.setframerate(RATE);w.writeframes(click.tobytes())
    guide=array.array('h',[0])*(total*2)
    beats=2 if config['meter']=='6/8' else int(config['meter'].split('/')[0])
    words=['um','dois','três','quatro']
    for i in range(beats):
        spoken=root/f'number-{i}.wav';converted=root/f'voice-{i}.wav'
        command(['espeak-ng','-v','pt-br','-s','180','-w',str(spoken),words[i]],30)
        command(['ffmpeg','-y','-i',str(spoken),'-ar',str(RATE),'-ac','1','-c:a','pcm_s16le',str(converted)],30)
        with wave.open(str(converted),'rb') as w:audio=array.array('h',w.readframes(w.getnframes()))
        if sys.byteorder!='little':audio.byteswap()
        start=round(i*60/config['bpm']*RATE)
        # Keep each spoken count before the next pulse.
        for n,value in enumerate(audio[:round(60/config['bpm']*RATE)]):
            if start+n>=total:break
            guide[(start+n)*2]=round(value*.55);guide[(start+n)*2+1]=round(value*.55)
    if sys.byteorder!='little':guide.byteswap()
    with wave.open(str(root/'Guia.wav'),'wb') as w:
        w.setnchannels(2);w.setsampwidth(2);w.setframerate(RATE);w.writeframes(guide.tobytes())
    return prefix,total

def process(job):
    stop=threading.Event();lost=threading.Event()
    def renew():
        while not stop.wait(20):
            try:call('heartbeat',job,{})
            except Exception:lost.set();return
    thread=threading.Thread(target=renew,daemon=True);thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='pa-separation-') as temp:
            root=pathlib.Path(temp);source=root/('source'+pathlib.Path(job['name']).suffix.lower())
            url=BASE+'/api/separation-worker?'+urllib.parse.urlencode({'action':'source','id':job['id'],'lease':job['lease']})
            with urllib.request.urlopen(urllib.request.Request(url,headers={'Authorization':'Bearer '+KEY,'User-Agent':USER_AGENT}),timeout=120) as r,source.open('wb') as dest:
                size=0
                while chunk:=r.read(1024**2):
                    size+=len(chunk)
                    if size>job['size']:raise ValueError('Input exceeds declared size')
                    dest.write(chunk)
                if size!=job['size']:raise ValueError('Incomplete source')
            info=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','json',str(source)],timeout=30))
            duration=float(info['format']['duration'])
            if not math.isfinite(duration) or not 0<duration<=1800:raise ValueError('Audio must be no longer than 30 minutes')
            config=job['config'];model,quality_args=separation_settings(config)
            command([sys.executable,'-m','demucs.separate','-n',model,'--float32',*quality_args,'--device',os.environ.get('SEPARATION_DEVICE','cpu'),'-o',str(root/'stems'),str(source)])
            if lost.is_set():raise RuntimeError('Lease lost')
            out=root/'prepared';out.mkdir();prefix,total=cues(out,config,duration)
            files=[]
            families=['vocals','drums','bass','other']+(['guitar','piano'] if model.endswith('6s') else [])
            stems={family:root/'stems'/model/source.stem/(family+'.wav') for family in families}
            analysis=analyze_stems(root,stems)
            for family in families:
                stem=root/'stems'/model/source.stem/(family+'.wav');target=out/(NAMES[family]+'.wav')
                # Identical prefix and sample count on all musical stems; no independent stretching.
                delay=round(prefix*RATE)
                command(['ffmpeg','-y','-i',str(stem),'-af',f'volume=0.7,adelay={delay}S:all=1,apad,atrim=end_sample={total}','-ar',str(RATE),'-ac','2','-c:a','pcm_s24le',str(target)])
                files.append((family,target))
            files.extend([('click',out/'Click.wav'),('guide',out/'Guia.wav')])
            archive=out/'Tracks.zip'
            with zipfile.ZipFile(archive,'w',zipfile.ZIP_STORED,allowZip64=True) as z:
                for family,path in files:
                    if family not in analysis or analysis[family]['present']:z.write(path,NAMES[family]+path.suffix)
                z.writestr('Analise-das-faixas.json',json.dumps(analysis,ensure_ascii=False,indent=2))
                z.writestr('LEIA-ME.txt','Faixas estimadas por IA. PCM24 nos instrumentos, PCM16 no click/guia, 44.1kHz estéreo. Um compasso inicial. BPM constante: revise o sincronismo. Click e guia devem ir apenas para o retorno esquerdo.\n')
            files.append(('zip',archive))
            for family,path in files:
                if lost.is_set():raise RuntimeError('Lease lost')
                # Stable ASCII output names allow safe download headers.
                safe=(NAMES.get(family) or 'Tracks')+path.suffix
                f=call('begin-output',job,{'name':safe,'family':family,'size':path.stat().st_size})
                if f['ready']:continue
                parts=[]
                with path.open('rb') as stream:
                    index=1
                    while chunk:=stream.read(PART):
                        if lost.is_set():raise RuntimeError('Lease lost')
                        parts.append(call('output-part',job,chunk,file=f['id'],part=index));index+=1
                call('complete-output',job,{'file':f['id'],'parts':parts})
            call('done',job,{'analysis':analysis,'quality':config.get('quality','refined'),'processorVersion':2})
    except Exception as e:
        try:call('failed',job,{'message':'Não foi possível separar esta gravação. Confira formato, duração, espaço e conexão do processador.'})
        except Exception:pass
        print('Separação interrompida:',type(e).__name__,flush=True)
        raise RuntimeError('Audio separation failed') from None
    finally:stop.set();thread.join(timeout=1)
def main():
    if not BASE.startswith('https://') or not KEY:raise SystemExit('Configure PLATFORM_URL e TRACK_PROCESSOR_KEY.')
    while True:
        try:
            job=call('claim',data={})['job']
            if job:process(job)
            else:time.sleep(5)
        except Exception:print('Separador aguardando conexão.',flush=True);time.sleep(10)
if __name__=='__main__':main()
