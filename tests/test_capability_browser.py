"""真实 Chromium 验证能力页、回放按钮、故障态和外部文本转义。"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
for folder in (ROOT/'website',ROOT/'bot'):
    sys.path.append(str(folder))

@unittest.skipUnless(os.getenv('CAPABILITY_BROWSER_TEST')=='1','explicit browser test')
class CapabilityBrowserTests(unittest.TestCase):
    def test_capability_page_refresh_evaluate_and_xss_in_real_chromium(self):
        from flask import Flask,jsonify,session,redirect,request,send_from_directory
        from werkzeug.serving import make_server
        import ceylan_dashboard
        chrome=next((p for p in (shutil.which('google-chrome'),
                     'C:/Program Files/Google/Chrome/Application/chrome.exe',
                     'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
                     if p and Path(p).is_file()),None)
        self.assertIsNotNone(chrome)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root,patch.dict(os.environ,{'DATA_DIR':root,'PERSONA_DATA_DIR':root}):
            app=Flask(__name__,static_folder=str(ROOT/'website'/'static'))
            app.secret_key='isolated-browser-only'
            ceylan_dashboard.install(app,{})
            hits=[]
            @app.before_request
            def fixtures():
                if request.path=='/fixture':
                    session['ceylan_admin']=True;session['bot_control_csrf']='test-token'
                    return redirect('/admin/dashboard')
                if request.path=='/admin/api/bot/qingyi/capabilities':
                    return jsonify(ok=True,capabilities={'active_memory':{
                        'implemented':True,'wired':True,'exercised':False,'evaluated':False,'released':False}},
                        requests=[{'scene':'group','request_sha256':'a'*64,'runtime':{'status':'partial'},
                                   'selected':[{'id':'<img src=x onerror="window.xss=true">','kind':'reported','visibility':'group','present':True}]}],
                        evaluation={'total':2,'passed':2,'failed':0,'cases':[]},worldbook={'entries':193},governance={'assets':[]})
                if request.path=='/admin/api/bot/qingyi/capabilities/evaluate':
                    hits.append(request.headers.get('X-CSRF-Token'))
                    return jsonify(ok=True,evaluation={'total':2,'passed':2,'failed':0})
                if request.path.startswith('/admin/api/'):
                    return jsonify(ok=True,control_csrf='test-token',tasks=[],stages=[],next={})
            driver='''<script>
window.addEventListener('load',async function(){
 try{
  function wait(f){return new Promise(function(resolve,reject){var end=Date.now()+12000;var t=setInterval(function(){if(f()){clearInterval(t);resolve();}else if(Date.now()>end){clearInterval(t);reject(new Error('timeout'));}},50);});}
  document.querySelector('[data-view="qingyi"]').click();
  document.querySelector('[data-qingyi-tab="capabilities"]').click();
  await wait(function(){return document.getElementById('capabilities-list').textContent.includes('active_memory');});
  if(document.querySelector('#capabilities-cases img'))throw new Error('XSS element created');
  if(window.xss)throw new Error('XSS executed');
  document.getElementById('capabilities-refresh').click();
  document.getElementById('capabilities-evaluate').click();
  await wait(function(){return document.getElementById('capabilities-msg').textContent.includes('2/2');});
  document.documentElement.dataset.capabilityBrowser='passed';
 }catch(e){document.documentElement.dataset.capabilityError=e.message;}
});</script>'''
            @app.after_request
            def inject(response):
                if request.path=='/admin/dashboard':
                    response.set_data(response.get_data(as_text=True).replace('</body>',driver+'</body>'))
                return response
            server=make_server('127.0.0.1',0,app,threaded=True)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                result=subprocess.run([chrome,'--headless=new','--disable-gpu','--disable-extensions',
                    '--no-first-run','--no-default-browser-check','--user-data-dir='+str(Path(root)/'browser'),
                    '--virtual-time-budget=20000','--window-size=1440,1000','--dump-dom',
                    'http://127.0.0.1:%d/fixture'%server.server_port],capture_output=True,text=True,
                    encoding='utf-8',errors='replace',timeout=90)
                self.assertIn('data-capability-browser="passed"',result.stdout,result.stdout[-2000:]+result.stderr[-1000:])
                self.assertEqual(['test-token'],hits)
            finally:
                server.shutdown();server.server_close();thread.join(timeout=3)

if __name__=='__main__':unittest.main()
