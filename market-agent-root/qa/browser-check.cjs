const {chromium}=require('playwright');
const fs=require('fs'),os=require('os'),path=require('path'),{spawn}=require('child_process');
const root=path.resolve(__dirname,'..'),temp=fs.mkdtempSync(path.join(os.tmpdir(),'market-qa-')),token='browser-test-token-'.repeat(3);
const server=spawn(process.env.QA_PYTHON || path.join(root,'.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python'),['-m','market_agent','serve','--port','8765'],{cwd:root,env:{...process.env,API_TOKEN:token,DATABASE_PATH:temp+'/qa.sqlite3',MARKETS_FILE:root+'/markets.live.json',MODE:'demo',SCHEDULE_ENABLED:'false',NEWS_COLLECTION_ENABLED:'false',LLM_ENABLED:'false'},stdio:'ignore'});
process.on('exit',()=>server.kill());
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE || undefined,headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});let errors=[];page.on('pageerror',e=>errors.push(e.message));
 for(let i=0;i<50;i++){try{await page.goto('http://127.0.0.1:8765/');break}catch(e){if(i===49)throw e;await new Promise(r=>setTimeout(r,100))}}
 async function login(t){await page.locator('#token').fill(t);await page.locator('#loginForm button').click()}
 await login('wrong');await page.waitForFunction(()=>document.querySelector('#message').textContent.includes('token'));
 await login(token);await page.locator('#app').waitFor({state:'visible'});
 await page.waitForFunction(()=>document.querySelector('#profileState').textContent.includes('Заполните'));
 if(!await page.locator('#run').isDisabled())throw Error('Missing profile did not block run');
 await page.locator('#cash').fill('100000');await page.locator('#horizon').fill('90');await page.locator('#risk').selectOption('conservative');
 await page.locator('#portfolioKnown').check();await page.locator('#addHolding').click();await page.locator('#holdings select').selectOption('SBER');await page.locator('#holdings input').fill('100');
 await page.locator('#notes').fill('Предпочитаю понятные компании');await page.locator('#saveProfile').click();
 await page.waitForFunction(()=>!document.querySelector('#run').disabled);
 const saved=await page.evaluate(async t=>(await fetch('/api/profile',{headers:{Authorization:'Bearer '+t}})).json(),token);
 if(saved.saved.profile.holdings[0].shares!==100||saved.saved.profile.horizon_days!==90)throw Error('Profile not persisted');
 await page.locator('#cash').fill('110000');if(!await page.locator('#run').isDisabled())throw Error('Unsaved profile not blocked');
 await page.locator('#refresh').click();if(!await page.locator('#run').isDisabled())throw Error('Refresh ignored dirty profile');
 await page.locator('#saveProfile').click();await page.waitForFunction(()=>!document.querySelector('#run').disabled);
 await page.locator('#check').click();await page.waitForFunction(()=>document.querySelector('#checks').textContent.includes('demo'));
 await page.locator('#run').click();await page.waitForFunction(()=>document.querySelector('#report').textContent.includes('План по сохранённым условиям'),{},{timeout:20000});
 if(await page.locator('#report table tr').count()!==21)throw Error('Not 20 tickers');
 if(await page.locator('#report td.buy,#report td.sell').count())throw Error('Demo made actionable recommendations');
 await page.screenshot({path:temp+'/desktop.png',fullPage:true});
 await page.reload();await page.locator('#login').waitFor({state:'visible'});if(await page.locator('#token').inputValue())throw Error('Token persisted');
 await login(token);await page.waitForFunction(()=>document.querySelector('#cash').value==='110000');
 if(await page.locator('#holdings input').inputValue()!=='100')throw Error('Holdings not restored');
 await page.locator('#history button').first().click();await page.waitForFunction(()=>document.querySelector('#report table'));

 // Display-only fixture: validates rendering of buy/sell fields, not real model quality.
 const runs=await page.evaluate(async t=>(await fetch('/api/runs',{headers:{Authorization:'Bearer '+t}})).json(),token);
 const fixture=await page.evaluate(async ({t,id})=>(await fetch('/api/runs/'+id,{headers:{Authorization:'Bearer '+t}})).json(),{t:token,id:runs[0].id});
 Object.assign(fixture.report.instruments[0].recommendation,{action:'buy',proposed_action:'buy',shares:10,lots:10,entry_min:99,entry_max:101,review_stop_price:95,estimated_cash_rub:1011.01,reasons:['Тест отображения покупки']});
 Object.assign(fixture.report.instruments[1].recommendation,{action:'sell',proposed_action:'sell',shares:20,lots:2,estimated_cash_rub:1998,reasons:['Тест отображения продажи']});
 await page.route('**/api/runs/'+runs[0].id,route=>route.fulfill({json:fixture}));
 await page.locator('#history button').first().click();
 await page.waitForFunction(()=>document.querySelectorAll('#report td.buy').length===1 && document.querySelectorAll('#report td.sell').length===1);
 if(!await page.locator('#report').textContent().then(t=>t.includes('10 шт.')&&t.includes('20 шт.')&&t.includes('Диапазон для проверки')))throw Error('Action details missing');
 await page.setViewportSize({width:390,height:844});if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Horizontal overflow');
 await page.screenshot({path:temp+'/mobile.png',fullPage:true});
 await page.locator('#holdings button').click();await page.locator('#portfolioKnown').uncheck();await page.locator('#saveProfile').click();
 await page.waitForFunction(()=>document.querySelector('#profileState').textContent.includes('Сохранено'));
 await page.locator('#logout').click();await page.locator('#login').waitFor({state:'visible'});
 if(errors.length)throw Error(errors.join(';'));
 console.log('PASS: login/auth, profile/holdings save+reload, dirty form gate, diagnostics, 20 ticker demo report without actions, history, buy/sell display fixture, mobile layout, logout.');
 await browser.close();server.kill();
})().catch(e=>{console.error(e);process.exit(1)});
