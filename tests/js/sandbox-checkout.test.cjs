/* Pure JS tests with synthetic DOM/Stripe objects. No browser or provider calls. */
const {readFileSync}=require('node:fs');
const {runInNewContext}=require('node:vm');
const assert=require('node:assert/strict');
const source=readFileSync('app/static/sandbox-checkout.js','utf8');
function setup(responses, failMount=false, storage=new Map()) {
 const form={events:{},values:{check_in:'2027-01-01',check_out:'2027-01-03',guests:'2',name:'Synthetic',email:'synthetic@example.invalid',language:'en'},
  addEventListener(name,handler){this.events[name]=handler;},querySelector(name){return name==='fieldset'?fields:next;}};
 const fields={disabled:false},next={disabled:false,textContent:''};
 const button=()=>({hidden:true,disabled:false,events:{},addEventListener(name,handler){this.events[name]=handler;}});
 const pay=button(),check=button(),resume=button(),fresh=button(),status={textContent:''},calls=[],confirmed=[];
 const root={dataset:{propertyId:'1',stripeKey:'pk_test_synthetic',checkoutId:'synthetic-checkout-id',resumeToken:'synthetic_seed_access'},querySelector(name){return {'[data-booking]':form,'[data-pay]':pay,'[data-check]':check,'[data-resume]':resume,'[data-new]':fresh,'[role="status"]':status,'[data-payment]':{}}[name];}};
 const stripe={elements(){return {create(){return {mount(){if(failMount){failMount=false;throw new Error('Synthetic element load failure');}}};}};},async confirmPayment(options){confirmed.push(options);return {paymentIntent:{status:'succeeded'}};}};
 runInNewContext(source,{document:{querySelectorAll:()=>[root]},window:{Stripe:()=>stripe,addEventListener(){}},
  FormData:class {constructor(form){return Object.entries(form.values);}},URLSearchParams,crypto:{randomUUID:()=> 'synthetic-checkout-id'},
  sessionStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},setTimeout:resolve=>resolve(),fetch:async(path,options)=>{calls.push({path,options});const value=responses.shift();if(value instanceof Error)throw value;return {ok:true,json:async()=>({result:value})};}});
 return {form,fields,next,pay,check,resume,fresh,status,calls,confirmed,storage,submit:()=>form.events.submit({preventDefault(){}})};
}
const quote={bookable:true,total:'200.00',quote_token:'synthetic-quote'};
const checkout={payment_intent_id:'pi_synthetic',client_secret:'synthetic_client_secret',access_token:'synthetic_access',amount:'200.00'};
(async()=>{
 const retry=setup([quote,new Error('Synthetic network loss'),checkout,{state:'confirmed',booking_ref:9}]);
 await retry.submit();await retry.submit();
 assert.equal(retry.fields.disabled,true);
 retry.form.values.name='Changed after ambiguous response';retry.form.events.input();
 await retry.submit();
 assert.equal(retry.calls[1].options.body,retry.calls[2].options.body,'Retry must retain exact UUID and immutable payload');
 assert.equal(retry.confirmed.length,0,'Preparing Elements must not confirm payment');
 await retry.pay.events.click();
 assert.equal(retry.confirmed.length,1);
 assert.match(retry.status.textContent,/confirmed.*9/);
 assert.equal(retry.calls[3].options.headers.Authorization,'Bearer synthetic_seed_access');
 assert(!retry.calls.some(call=>call.path.includes('synthetic_access')||call.path.includes('client_secret')));
 const mount=setup([quote,checkout,{state:'refunded'}],true);
 await mount.submit();await mount.submit();
 assert.equal(mount.pay.hidden,false,'An interrupted Element mount must expose recovery');
 await mount.pay.events.click();assert.match(mount.status.textContent,/refunded/);
 const refund=setup([quote,checkout,{state:'refund_failed'}]);
 await refund.submit();await refund.submit();await refund.pay.events.click();
 assert.match(refund.status.textContent,/not complete/);assert.equal(refund.check.hidden,false);
 const storage=new Map();
 const original=setup([quote,checkout],false,storage);await original.submit();await original.submit();
 const stored=JSON.stringify([...storage.values()]);
 assert(!stored.includes('synthetic@example.invalid') && !stored.includes('client_secret') && !stored.includes('Synthetic'));
 const restored=setup([{...checkout,provider_state:'succeeded'},{state:'confirmed',booking_ref:15}],false,storage);
 assert.equal(restored.fields.disabled,true);assert.equal(restored.resume.hidden,false);
 await restored.resume.events.click();
 assert.equal(restored.confirmed.length,0,'Restoring a successful intent must not attempt to pay again');
 assert(restored.calls[0].path.endsWith('/resume'));
 assert(!restored.calls.some(call=>call.path==='/api/public/booking'));
 assert.match(restored.status.textContent,/confirmed.*15/);
 const invalid=new Map([['omi-sandbox-checkout-v1:1','not valid JSON']]);
 const blocked=setup([],false,invalid);assert.equal(blocked.next.disabled,true);assert.equal(blocked.calls.length,0);
 console.log('5 synthetic checkout browser-logic scenarios passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
