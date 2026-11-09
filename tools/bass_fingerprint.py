"""Bass-Fingerabdruck: Obertöne der 16tel-3-Noten (Bass minus Hintergrund davor), Abklingen, Pegel."""
import sys, numpy as np, soundfile as sf
from scipy.signal import butter, sosfilt
beat=60/129; st=beat/4
def fp(path, f0=73.4):
    x,sr=sf.read(path,always_2d=True); m=x.mean(1)
    N=1<<16; H=[]
    E_lo=[];E_mid=[]
    for b in range(1,9):
        on=b*beat+2*st
        if int((on+st)*sr)>len(m): break
        def spec(t0,dur):
            a=int(t0*sr); s=m[a:a+int(dur*sr)]; S=np.abs(np.fft.rfft(s*np.hanning(len(s)),N))**2
            return [S[int(round(f0*h*N/sr))-3:int(round(f0*h*N/sr))+4].max() for h in range(1,7)]
        note=np.array(spec(on+0.005,0.05)); bg=np.array(spec(on-0.055,0.05))
        H.append(np.maximum(note-bg,1e-12))
        lo=sosfilt(butter(4,[60,90],'bandpass',fs=sr,output='sos'),m[int(on*sr):int((on+st)*sr)])
        mi=sosfilt(butter(4,[200,2000],'bandpass',fs=sr,output='sos'),m[int(on*sr):int((on+st)*sr)])
        blk=int(0.01*sr); E_lo.append(np.sqrt(np.mean(lo[:len(lo)//blk*blk].reshape(-1,blk)**2,1))); E_mid.append(np.sqrt(np.mean(mi[:len(mi)//blk*blk].reshape(-1,blk)**2,1)))
    H=np.mean(H,0); h=10*np.log10(H/H[0])
    el=20*np.log10(np.mean(E_lo,0)+1e-12); em=20*np.log10(np.mean(E_mid,0)+1e-12)
    return {'harm h2..h6 dB':np.round(h[1:],1).tolist(),
            'Grundton 60-90Hz Abfall nach 50/100ms':[round(el[5]-el[:3].max(),1),round(el[10]-el[:3].max(),1)] if len(el)>10 else None,
            'Mitten 200-2k Abfall nach 50/100ms':[round(em[5]-em[:3].max(),1),round(em[10]-em[:3].max(),1)] if len(em)>10 else None,
            'Pegel Grundton dB':round(10*np.log10(H[0]),1)}
if __name__=='__main__':
    for p in sys.argv[1:]: print(p.split('/')[-1][:40], fp(p))
