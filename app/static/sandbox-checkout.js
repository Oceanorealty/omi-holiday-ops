/* Sandbox only. Card data is collected by Stripe Elements, never by OPS. */
(() => {
  'use strict';
  document.querySelectorAll('[data-omi-checkout]').forEach(root => {
    const form = root.querySelector('[data-booking]');
    const fields = form.querySelector('fieldset');
    const next = form.querySelector('button');
    const pay = root.querySelector('[data-pay]');
    const check = root.querySelector('[data-check]');
    const status = root.querySelector('[role="status"]');
    const propertyId = Number(root.dataset.propertyId);
    let quote, attempt, checkout, stripe, elements, paymentElement, busy = false, terminal = false, submitted = false;
    const tell = value => { status.textContent = value; };
    const request = async (path, options = {}) => {
      const response = await fetch(path, {credentials:'omit', cache:'no-store', ...options});
      let body;
      try { body = await response.json(); } catch (_) { throw new Error('Connection interrupted. Retry this same attempt; do not start another payment.'); }
      if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'Check the test booking details.');
      return body.result;
    };
    form.addEventListener('input', () => {
      if (attempt || busy) return;
      quote = undefined; next.textContent = 'Get quote';
    });
    window.addEventListener('beforeunload', event => {
      if (attempt && !terminal) { event.preventDefault(); event.returnValue = ''; }
    });
    const mountPayment = () => {
      stripe ||= window.Stripe(root.dataset.stripeKey);
      elements ||= stripe.elements({clientSecret:checkout.client_secret});
      if (!paymentElement) { const element = elements.create('payment'); element.mount(root.querySelector('[data-payment]')); paymentElement = element; }
      pay.hidden = false; check.hidden = false; next.disabled = true;
      tell(`Test payment: AUD ${checkout.amount}. Use a sandbox card only.`);
    };
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (busy || checkout) return;
      if (!Number.isInteger(propertyId) || propertyId < 1 || !/^pk_test_/.test(root.dataset.stripeKey || '') || !window.Stripe) {
        tell('Sandbox checkout configuration or Stripe.js is unavailable.'); return;
      }
      busy = true; next.disabled = true;
      try {
        if (!quote) {
          const values = Object.fromEntries(new FormData(form));
          fields.disabled = true;
          quote = await request('/api/public/quote?' + new URLSearchParams({property_id:propertyId, check_in:values.check_in, check_out:values.check_out, guests:values.guests}));
          if (!quote.bookable) { const reason = quote.reason; quote = undefined; throw new Error(reason || 'This stay is unavailable.'); }
          next.textContent = 'Continue to test payment'; tell(`Total AUD ${quote.total}. Continue to enter a test card.`);
        } else {
          if (!attempt) {
            const values = Object.fromEntries(new FormData(form));
            attempt = {checkout_id:crypto.randomUUID(), property_id:propertyId, check_in:values.check_in, check_out:values.check_out,
              guests:Number(values.guests), guest:{name:values.name, email:values.email, language:values.language}, quote_token:quote.quote_token};
            fields.disabled = true; // Freeze the exact payload and UUID across ambiguous network retries.
          }
          checkout = await request('/api/public/booking', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(attempt)});
          mountPayment();
        }
      } catch (error) {
        if (checkout) { pay.hidden = false; check.hidden = false; }
        next.textContent = attempt ? 'Retry same checkout' : 'Get quote'; tell(error.message);
      } finally { busy = false; fields.disabled = Boolean(attempt); next.disabled = Boolean(checkout); }
    });
    const poll = async () => {
      for (let i=0; i<30; i++) {
        const result = await request('/api/public/booking/' + encodeURIComponent(checkout.payment_intent_id), {headers:{Authorization:'Bearer '+checkout.access_token}});
        if (result.state === 'confirmed' || result.state === 'refunded') {
          terminal = true; pay.hidden = true; check.hidden = true;
          tell(result.state === 'confirmed' ? `Test booking confirmed. Reference: ${result.booking_ref}` : 'The stay was unavailable. The sandbox payment was refunded.'); return;
        }
        if (['refund_pending','refund_failed'].includes(result.state)) {
          pay.hidden = true; tell('No booking was created. Refund is not complete; the test operator must review it.'); return;
        }
        await new Promise(resolve => setTimeout(resolve, 2000));
      }
      tell('Confirmation is still pending. Use Check booking status or contact the test operator. Do not pay again.');
    };
    pay.addEventListener('click', async () => {
      if (busy || !checkout || submitted || terminal) return;
      busy = true; pay.disabled = true; check.disabled = true;
      try {
        if (!elements) mountPayment();
        const result = await stripe.confirmPayment({elements, redirect:'if_required'});
        if (result.error) throw new Error(result.error.message);
        submitted = true; pay.hidden = true;
        tell('Payment submitted. Waiting for the signed callback…'); await poll();
      } catch (error) { tell(error.message); }
      finally { busy = false; pay.disabled = submitted; check.disabled = false; }
    });
    check.addEventListener('click', async () => {
      if (busy || !checkout || terminal) return;
      busy = true; check.disabled = true; pay.disabled = true;
      try { await poll(); } catch (error) { tell(error.message); }
      finally { busy = false; check.disabled = false; pay.disabled = submitted; }
    });
  });
})();
