// Browser-runtime smoke checks without a browser installation or real user data.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('miniapp/app.js','utf8');
function environment({sdk=true,brokenSdk=false,networkError=false}={}){
  const nodes=new Map();
  const node=()=>({innerHTML:'',textContent:'',hidden:true,disabled:false,style:{setProperty(){}},classList:{toggle(){},contains(){return false;}}});
  const document={querySelector(selector){if(!nodes.has(selector))nodes.set(selector,node());return nodes.get(selector);},body:node(),documentElement:node(),addEventListener(name,fn){this[name]=fn;}};
  const result={profile:{id:1,name:'Test Owner',email:'test@example.com'},admin:true,calendar:false,pending:0,demo:false,today:'2026-10-28',settings:{timezone:'Asia/Kamchatka',weekdays:[0,1,2,3,4],horizon_days:30}};
  const sandbox={document,console,setTimeout,clearTimeout,AbortController,Intl,URLSearchParams,crypto:require('node:crypto').webcrypto,
    async fetch(){if(networkError)throw new Error('Network unavailable');return {ok:true,json:async()=>result};},scrollTo(){}};
  if(sdk){const call=()=>{if(brokenSdk)throw new Error('Unsupported SDK method');};sandbox.Telegram={WebApp:{initData:'test',colorScheme:'light',ready:call,expand:call,onEvent:call,BackButton:{hide:call,show:call,onClick:call}}};}
  sandbox.window=sandbox;
  // Window.top is an unforgeable property. The original global function top failed here.
  Object.defineProperty(sandbox,'top',{get(){return sandbox;},configurable:false});
  vm.createContext(sandbox);
  return {sandbox,document,nodes};
}
async function check(options){const env=environment(options);vm.runInContext(source,env.sandbox);await new Promise(setImmediate);return env;}
(async()=>{
  for(const options of [{},{sdk:false},{brokenSdk:true}]){
    const env=await check(options),app=env.nodes.get('#app');
    assert.match(app.innerHTML,/Новая заявка/,'startup must replace loading screen');
    assert.match(app.innerHTML,/Управление/);
    await env.document.click({target:{closest:()=>({dataset:{action:'new'},disabled:false})}});
    assert.match(app.innerHTML,/Календарь не подключён/,'new booking button must respond');
    assert.equal(env.sandbox.top,env.sandbox,'native Window.top must remain intact');
  }
  const failed=await check({networkError:true});
  assert.match(failed.nodes.get('#app').innerHTML,/Попробовать снова/,'network failure must offer retry');
  console.log('Client startup, native Window.top, menu click, missing/unsupported SDK and network retry: OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
