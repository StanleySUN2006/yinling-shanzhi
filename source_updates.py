"""Check registered official sources; preserve reviewed numeric baseline on every outcome."""
import concurrent.futures
import hashlib
import json
import re
import urllib.error
import urllib.request
from datetime import datetime,timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin,urlsplit

SOURCES=[
 {'id':'population_2025','title':'户籍老年人口','file':'population_2025.html','tokens':['2025','户籍','老年人口'],'url':'https://mzj.sh.gov.cn/2026bsmz/20260515/aaa41ec3fec949c79d7958aad878aa6f.html'},
 {'id':'basic_beds_2026','title':'保基本床位名单','file':'basic_beds_2026.html','tokens':['2026','保基本','床位'],'url':'https://mzj.sh.gov.cn/2026bsmz/20260722/8c9c94c58ece4859ac4c6675f398584a.html'},
 {'id':'basic_policy','title':'保基本床位管理办法','file':'basic_policy.html','tokens':['第六条','1.5%','户籍'],'url':'https://mzj.sh.gov.cn/MZ_zhuzhan2739_0-2-8-15-55/20221231/7b0f176b5fc7417082eadd01c1421313.html'},
]

class ArticleParser(HTMLParser):
 def __init__(self):super().__init__();self.depth=0;self.text=[];self.images=[];self.found=False
 def handle_starttag(self,tag,attrs):
  a=dict(attrs)
  if tag=='div':
   if not self.depth and a.get('id')=='ivs_content':self.depth=1;self.found=True
   elif self.depth:self.depth+=1
  if self.depth and tag=='img' and a.get('src'):self.images.append(a['src'])
 def handle_endtag(self,tag):
  if tag=='div' and self.depth:self.depth-=1
 def handle_data(self,text):
  if self.depth:self.text.append(text)

def extract(raw,url,tokens):
 p=ArticleParser();p.feed(raw.decode('utf-8-sig'))
 text=re.sub(r'\s+','', ''.join(p.text))
 if not p.found or not all(t in text for t in tokens):raise ValueError('未识别到完整正文或必要字段')
 images=[urljoin(url,x) for x in p.images]
 if any(urlsplit(x).scheme!='https' or urlsplit(x).hostname!='mzj.sh.gov.cn' for x in images):raise ValueError('正文图片来源超出登记范围')
 return {'text':text,'images':images}

def sha(value):return hashlib.sha256(value).hexdigest()

class OfficialRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,req,fp,code,msg,headers,newurl):
  target=urljoin(req.full_url,newurl)
  if urlsplit(target).scheme!='https' or urlsplit(target).hostname!='mzj.sh.gov.cn':raise ValueError('重定向目标不在登记范围')
  return super().redirect_request(req,fp,code,msg,headers,target)

def fetch(url):
 if urlsplit(url).scheme!='https' or urlsplit(url).hostname!='mzj.sh.gov.cn':raise ValueError('不允许请求未登记网站')
 req=urllib.request.Request(url,headers={'User-Agent':'YinlingResearch/1.0','Accept':'text/html,image/*'})
 with urllib.request.build_opener(OfficialRedirect()).open(req,timeout=10) as f:
  if urlsplit(f.geturl()).hostname!='mzj.sh.gov.cn':raise ValueError('页面重定向到未登记网站')
  body=f.read(5_000_001)
  if len(body)>5_000_000:raise ValueError('来源文件超过检查大小限制')
  return body

def compare_source(spec,raw_dir,fetcher=fetch):
 result={'source_id':spec['id'],'title':spec['title'],'url':spec['url'],'status':'unverified','changes':[],'errors':[]}
 try:
  old_raw=(raw_dir/spec['file']).read_bytes();old=extract(old_raw,spec['url'],spec['tokens'])
  new_raw=fetcher(spec['url']);new=extract(new_raw,spec['url'],spec['tokens'])
  result.update(old_article_hash=sha(old['text'].encode()),new_article_hash=sha(new['text'].encode()),old_images=len(old['images']),new_images=len(new['images']))
  if old['text']!=new['text']:result['changes'].append('正文变化')
  if old['images']!=new['images']:result['changes'].append('图片清单或链接变化')
  # Check image content, not just its URL: a PNG can be replaced in place.
  def image_check(pair):
   i,url=pair;local=raw_dir/f"{spec['id']}_{i}.png"
   if url not in old['images']:return {'index':i,'status':'new_image'}
   old_index=old['images'].index(url);local=raw_dir/f"{spec['id']}_{old_index}.png"
   try:
    data=fetcher(url)
    if not data.startswith((b'\x89PNG\r\n\x1a\n',b'\xff\xd8\xff')):raise ValueError('图片响应格式异常')
    return {'index':i,'status':'same' if sha(local.read_bytes())==sha(data) else 'changed','old_hash':sha(local.read_bytes()),'new_hash':sha(data)}
   except Exception as e:return {'index':i,'status':'unverified','error':safe_error(e)}
  with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:images=list(executor.map(image_check,enumerate(new['images'])))
  result['image_checks']=images
  if any(x['status'] in ('changed','new_image') for x in images):result['changes'].append('图片数据变化')
  if any(x['status']=='unverified' for x in images):result['errors'].append('部分图片无法核验')
  result['status']='needs_review' if result['changes'] else ('unverified' if result['errors'] else 'unchanged')
 except Exception as e:result['errors'].append(safe_error(e))
 return result

def safe_error(error):
 if isinstance(error,urllib.error.HTTPError):return f'HTTP {error.code}，本次未完成核验'
 if isinstance(error,ValueError):return str(error)
 return f'{type(error).__name__}，本次未完成核验'

def check_sources(root,fetcher=fetch,persist=True):
 root=Path(root);path=root/'data/baseline.json';before=sha(path.read_bytes());raw=root/'data/raw/官方依据'
 previous_path=root/'data/source_refresh/latest.json'
 previous=json.loads(previous_path.read_text()) if previous_path.exists() else {}
 with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:results=list(executor.map(lambda s:compare_source(s,raw,fetcher),SOURCES))
 changed=any(x['status']=='needs_review' for x in results);complete=all(x['status']!='unverified' and not x['errors'] for x in results)
 review_required=changed or (previous.get('review_required',previous.get('changes_found',False)) and previous.get('baseline_sha256')==before)
 report={'checked_at':datetime.now(timezone.utc).isoformat(),'scope':'只检查已登记的三份官方页面及正文图片；不代表已发现最新年度或全部新政策',
  'sources':results,'complete':complete,'changes_found':changed,'baseline_sha256':before,'baseline_preserved':before==sha(path.read_bytes()),
  'numeric_baseline_updated':False,'review_required':review_required,'action':'来源变化尚待复核，数值底表未更新' if review_required else ('登记来源未发现变化，数值底表保持原版本' if complete else '本次核验不完整；保留已核验底表，不能宣称数据已更新')}
 if persist:
  out=root/'data/source_refresh';out.mkdir(parents=True,exist_ok=True)
  (out/'latest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
 return report

if __name__=='__main__':
 import argparse
 p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=Path(__file__).parent);a=p.parse_args()
 print(json.dumps(check_sources(a.root),ensure_ascii=False,indent=2))
