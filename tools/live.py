"""Direkter Socket-Client für das AbletonMCP Remote Script (Port 9877). Nutzung: from live import cmd"""
import socket, json, sys, time
def cmd(t, p=None, timeout=15, retry=6):
    for _i in range(retry):
        try:
            s=socket.create_connection(("localhost",9877),timeout=5); break
        except OSError:
            if _i==retry-1: raise
            time.sleep(1.5)
    s.sendall(json.dumps({"type":t,"params":p or {}}).encode())
    s.settimeout(timeout); buf=b""
    while True:
        try: c=s.recv(65536)
        except socket.timeout: break
        if not c: break
        buf+=c
        try: json.loads(buf.decode()); break
        except Exception: pass
    s.close(); return json.loads(buf.decode()) if buf else {"status":"error","message":"leer"}
def tracks():
    """{name: index} aller lesbaren Spuren (Gruppen liefern keinen Namen)."""
    n=cmd("get_session_info")["result"]["track_count"]; out={}
    for i in range(n):
        r=cmd("get_track_info",{"track_index":i}).get("result") or {}
        if r: out[r["name"]]=i
    return out
if __name__=="__main__":
    print(json.dumps(cmd(sys.argv[1], json.loads(sys.argv[2]) if len(sys.argv)>2 else {}),indent=1,ensure_ascii=False))
