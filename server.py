"""Loopback-only UI; private files are never served. Model jobs run outside request threads."""
import argparse
import copy
import json
import os
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from agent import Agent,AgentError

ROOT=Path(__file__).parent
# 公网隧道模式：由 Cloudflare Tunnel 转发时 Host/Origin 是访问域名而非 127.0.0.1。
PUBLIC_MODE=os.environ.get('YINLING_PUBLIC','').strip().lower() in ('1','true','yes')
# 可选：只接受指定域名访问，例如 yinling.example.com。留空表示接受任意域名（仍校验 Origin 同源 + CSRF）。
ALLOWED_HOST=os.environ.get('YINLING_ALLOWED_HOST','').strip().lower()

def host_allowed(handler):
    port=handler.server.server_address[1]
    host=(handler.headers.get('Host') or '').split(':')[0].lower()
    if PUBLIC_MODE:
        if ALLOWED_HOST:
            return host==ALLOWED_HOST.split(':')[0]
        return bool(host)
    return host in (f'127.0.0.1',f'localhost')

def origin_allowed(handler):
    origin=handler.headers.get('Origin')
    if not origin:
        return True
    origin_host=(urlsplit(origin).netloc or '').lower()
    if PUBLIC_MODE:
        host=(handler.headers.get('Host') or '').lower()
        if ALLOWED_HOST:
            return origin_host==ALLOWED_HOST
        return origin_host==host
    port=handler.server.server_address[1]
    return origin in (f'http://127.0.0.1:{port}',f'http://localhost:{port}')

class Session:
 def __init__(self):
  self.csrf=secrets.token_urlsafe(24);self.agent=None;self.job=None;self.last_result=None;self.turns=[];self.updated=time.monotonic();self.lock=threading.Lock()

def make_server(root=ROOT,port=8771,agent_factory=None):
 root=Path(root);sessions={};guard=threading.Lock();slots=threading.BoundedSemaphore(3)
 factory=agent_factory or (lambda:Agent(root))
 class Handler(BaseHTTPRequestHandler):
  def log_message(self,*args):pass
  def send(self,status,data,ctype='application/json; charset=utf-8',cookie=None,filename=None):
   raw=json.dumps(data,ensure_ascii=False).encode() if ctype.startswith('application/json') else data
   self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(raw)))
   self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
   self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
   if cookie:self.send_header('Set-Cookie',cookie)
   if filename:self.send_header('Content-Disposition',f'attachment; filename="{filename}"')
   self.end_headers();self.wfile.write(raw)
  def session(self,create=False):
   try:c=SimpleCookie(self.headers.get('Cookie',''));sid=c['yinling_session'].value if 'yinling_session' in c else ''
   except Exception:sid=''
   cookie=None
   with guard:
    for k in list(sessions):
     s=sessions[k]
     if time.monotonic()-s.updated>7200 and not(s.job and s.job['status']=='running'):del sessions[k]
    if sid not in sessions:
     if not create:return None,None
     if len(sessions)>=16:return None,None
     sid=secrets.token_urlsafe(24);sessions[sid]=Session();cookie=f'yinling_session={sid}; HttpOnly; SameSite=Strict; Path=/'
    s=sessions[sid];s.updated=time.monotonic()
   return s,cookie
  def valid_host(self):
   return host_allowed(self)
  def do_GET(self):
   if not self.valid_host():return self.send(403,{'error':'请求来源不被允许'})
   path=urlsplit(self.path).path
   files={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','application/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8')}
   if path in files:
    name,ctype=files[path];return self.send(200,(root/'static'/name).read_bytes(),ctype)
   if path=='/api/bootstrap':
    s,cookie=self.session(True)
    if s is None:return self.send(503,{'error':'会话数量已满，请稍后重试'})
    baseline=json.loads((root/'data/baseline.json').read_text());policy=json.loads((root/'data/policy.json').read_text())
    with s.lock:turns=copy.deepcopy(s.turns);job=copy.deepcopy(s.job)
    return self.send(200,{'csrf':s.csrf,'baseline':baseline,'policy':policy,'turns':turns,'job':job,'configured':(root/'.env').exists()},cookie=cookie)
   if path in ('/api/job','/api/export'):
    s,_=self.session()
    if s is None:return self.send(401,{'error':'请刷新页面建立会话'})
    with s.lock:
     if path=='/api/job':return self.send(200,copy.deepcopy(s.job))
     if not s.last_result:return self.send(404,{'error':'还没有可导出的结果'})
     return self.send(200,copy.deepcopy(s.last_result),filename='yinling-scenario.json')
   return self.send(404,{'error':'未找到页面'})
  def do_POST(self):
   if not self.valid_host():return self.send(403,{'error':'请求来源不被允许'})
   if not origin_allowed(self):return self.send(403,{'error':'请求来源不被允许'})
   s,_=self.session()
   if s is None:return self.send(401,{'error':'请刷新页面建立会话'})
   if not secrets.compare_digest(self.headers.get('X-CSRF-Token',''),s.csrf):return self.send(403,{'error':'会话校验失败，请刷新页面'})
   try:
    size=int(self.headers.get('Content-Length','0'))
    if not 0<size<=20000:raise ValueError()
    body=json.loads(self.rfile.read(size))
    if not isinstance(body,dict):raise ValueError()
   except (ValueError,UnicodeError):return self.send(400,{'error':'请求格式无效或内容过长'})
   path=urlsplit(self.path).path
   if path=='/api/reset':
    with s.lock:
     if s.job and s.job['status']=='running':return self.send(409,{'error':'请等待当前计算结束'})
     s.agent=None;s.job=None;s.last_result=None;s.turns=[]
    return self.send(200,{'ok':True})
   if path!='/api/chat':return self.send(404,{'error':'未找到接口'})
   prompt=body.get('prompt')
   if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>6000:return self.send(400,{'error':'请输入1至6000字的问题'})
   with s.lock:
    if s.job and s.job['status']=='running':return self.send(409,{'error':'已有任务在计算，请等待完成'})
    if not slots.acquire(blocking=False):return self.send(503,{'error':'当前任务较多，请稍后重试'})
    job={'id':secrets.token_hex(8),'status':'running','events':[],'result':None,'error':None};s.job=job
   def run():
    try:
     if s.agent is None:
      s.agent=factory();s.agent.messages[0]['content']+='\n网页已展示完整方案表和调用过程。最终解释控制在400字以内，不重复大表，不写Markdown表格。'
     def event(value):
      with s.lock:job['events'].append({**value,'time':round(time.time(),3)})
     result=s.agent.run(prompt,on_event=event)
     with s.lock:
      s.last_result=result;s.turns.append({'prompt':prompt,'result':result});job['result']=result;job['status']='done'
    except Exception as e:
     message=str(e) if isinstance(e,(AgentError,ValueError)) else '计算未完成，请重新开始情景后再试'
     with s.lock:job['status']='error';job['error']=message;s.agent=None
    finally:slots.release()
   threading.Thread(target=run,daemon=True).start();return self.send(202,{'job_id':job['id']})
 bind_host=os.environ.get('BIND_HOST','127.0.0.1')
 return ThreadingHTTPServer((bind_host,port),Handler)

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=int(os.environ.get('PORT','8771')));p.add_argument('--root',type=Path,default=ROOT);a=p.parse_args()
 server=make_server(a.root,a.port);print(f'银龄善治已启动：http://{os.environ.get("BIND_HOST","127.0.0.1")}:{server.server_address[1]}',flush=True)
 try:server.serve_forever()
 except KeyboardInterrupt:server.server_close()
