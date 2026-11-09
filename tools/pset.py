import sys, re
sys.path.insert(0,__import__('os').path.dirname(__file__))
from live import cmd
def params(ti,di=0): return {p['name']:p for p in cmd('get_device_parameters',{'track_index':ti,'device_index':di})['result']['parameters']}
def num(disp):
    d=disp.replace(',','.').strip()
    if 'inf' in d: return -1e9 if '-' in d else 1e9
    m=re.match(r'([+-]?[0-9.]+)\s*(k?Hz|ms|s|dB|%|st|ct)?',d)
    if not m: return None
    v=float(m.group(1)); u=m.group(2) or ''
    if u=='kHz': v*=1000
    if u=='s': v*=1000
    return v
def raw(ti,di,name,v):
    r=cmd('set_device_parameter',{'track_index':ti,'device_index':di,'param_name':name,'value':v})
    if r.get('status')!='success': raise Exception(str(r))
    return params(ti,di)[name]['display_value']
def setd(ti,name,target,di=0,tol=0.01):
    """set numeric param so its display ~ target (monotonic search)."""
    p=params(ti,di)[name]; lo,hi=p['min'],p['max']
    if p['is_quantized']:
        for v in range(int(lo),int(hi)+1):
            d=raw(ti,di,name,float(v))
            if (isinstance(target,str) and d.lower()==target.lower()) or (not isinstance(target,str) and num(d)==target): return d
        raise Exception(f'{name}: {target} not found')
    dlo=num(raw(ti,di,name,lo)); dhi=num(raw(ti,di,name,hi)); inc=dhi>dlo
    a,b=lo,hi
    for _ in range(40):
        m=(a+b)/2; d=num(raw(ti,di,name,m))
        if d is None: break
        if abs(d-target)<=max(tol*abs(target),1e-6): break
        if (d<target)==inc: a=m
        else: b=m
    return params(ti,di)[name]['display_value']
