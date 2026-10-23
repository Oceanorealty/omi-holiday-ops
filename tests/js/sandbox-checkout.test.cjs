/* Pure JS tests with synthetic DOM/Stripe objects. No browser or provider calls. */
const {readFileSync}=require('node:fs');
const {runInNewContext}=require('node:vm');
const assert=require('node:assert/strict');
const source=readFileSync('app/static/sandbox-checkout.js','utf8');
function setup(responses, failMount=false) {
 const form={events:{},values:{check_in:'2027-01-01',check_out:'2027-01-03',guests:'2',name:'Synthetic',email:'synthetic@example.invalid',language:'en'},
  addEventListener(name,handler){this.events[name]=handler;},querySelector(name){return name==='fieldset'?fields:next;}};
 const fields={disabled:false},next={disabled:false,textContent:''};
 const button=()=>({hidden:true,disabled:false,events:{},addEventListener(name,handler){this.events[name]=handler;}});
 const pay=button(),check=button(),status={textContent:''},calls=[],confirmed=[];
 const root={dataset:{propertyId:'1',stripeKey:'pk_test_synthetic'},querySelector(name){return {'[data-booking]':form,'[data-pay]':pay,'[data-check]':check,'[role="status"]':status,'[data-payment]':{}}[name];}};
 const stripe={elements(){return {create(){return {mount(){if(failMount){failMount=false;throw new Error('Synthetic element load failure');}}};}};},async confirmPayment(options){confirmed.push(options);return {paymentIntent:{status:'succeeded'}};}};
 runInNewContext(source,{document:{querySelectorAll:()=>[root]},window:{Stripe:()=>stripe,addEventListener(){}},
  FormData:class {constructor(form){return Object.entries(form.values);}},URLSearchParams,crypto:{randomUUID:()=> 'synthetic-checkout-id'},
  setTimeout:resolve=>resolve(),fetch:async(path,options)=>{calls.push({path,options});const value=responses.shift();if(value instanceof Error)throw value;return {ok:true,json:async()=>({result:value})};}});
 return {form,fields,next,pay,check,status,calls,confirmed,submit:()=>form.events.submit({preventDefault(){}})};
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
 assert.equal(retry.calls[3].options.headers.Authorization,'Bearer synthetic_access');
 assert(!retry.calls.some(call=>call.path.includes('synthetic_access')||call.path.includes('client_secret')));
 const mount=setup([quote,checkout,{state:'refunded'}],true);
 await mount.submit();await mount.submit();
 assert.equal(mount.pay.hidden,false,'An interrupted Element mount must expose recovery');
 await mount.pay.events.click();assert.match(mount.status.textContent,/refunded/);
 const refund=setup([quote,checkout,{state:'refund_failed'}]);
 await refund.submit();await refund.submit();await refund.pay.events.click();
 assert.match(refund.status.textContent,/not complete/);assert.equal(refund.check.hidden,false);
 console.log('3 synthetic checkout browser-logic scenarios passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
