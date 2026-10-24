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
    const holdMessage = root.querySelector('[data-hold-status]');
    const cancel = root.querySelector('[data-cancel]');
    let holdDeadline = 0, holdTimer;
    if (!holdMessage || !cancel) { status.textContent = 'Checkout changed. Reload this page before starting a payment.'; next.disabled = true; return; }
    const resume = root.querySelector('[data-resume]');
    const fresh = root.querySelector('[data-new]');
    const propertyId = Number(root.dataset.propertyId);
    let quote, attempt, checkout, stripe, elements, paymentElement, busy = false, terminal = false, submitted = false;
    const tell = value => { status.textContent = value; };
    const storageKey = 'omi-sandbox-checkout-v1:' + propertyId;
    let context = {id:root.dataset.checkoutId, token:root.dataset.resumeToken, propertyId, at:Date.now()};
    const saveContext = () => {
      if (!context.id || !context.token) throw new Error('Reload recovery is not configured; no payment was started.');
      // Per-tab storage contains an opaque checkout identity only, never guest/card details or client secrets.
      sessionStorage.setItem(storageKey, JSON.stringify(context));
    };
    const updateHold = result => {
      clearInterval(holdTimer);
      holdDeadline = result.hold_active && Number.isFinite(result.hold_seconds_remaining)
        ? performance.now() + result.hold_seconds_remaining * 1000 : 0;
      const render = () => {
        const seconds = Math.max(0, Math.ceil((holdDeadline - performance.now()) / 1000));
        if (seconds > 0) {
          holdMessage.textContent = `Local inventory held: ${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')} remaining. Refreshing does not extend the hold.`;
        } else {
          clearInterval(holdTimer); pay.disabled = true; pay.hidden = true;
          holdMessage.textContent = 'No active inventory hold. This does not prove payment failed. Check the original payment status before starting another payment.';
        }
      };
      render();
      if (holdDeadline > performance.now()) holdTimer = setInterval(render, 1000);
      return holdDeadline > performance.now();
    };
    const finish = result => {
      if (!['confirmed','refunded','cancelled'].includes(result.state)) return false;
      clearInterval(holdTimer);
      holdMessage.textContent = result.state === 'confirmed' ? 'Inventory is reserved by the confirmed test booking.' : 'The checkout hold has ended.';
      terminal = true; cancel.hidden = true; root.querySelector('[data-payment]').hidden = true; pay.hidden = true; check.hidden = true; resume.hidden = true; fresh.hidden = false;
      tell(result.state === 'confirmed' ? `Test booking confirmed. Reference: ${result.booking_ref}`
        : result.state === 'cancelled' ? 'Test checkout cancelled. No funds were received; the local inventory hold is released.'
        : 'The stay was unavailable. The sandbox payment was refunded.');
      return true;
    };
    const request = async (path, options = {}) => {
      const response = await fetch(path, {credentials:'omit', cache:'no-store', ...options});
      let body;
      try { body = await response.json(); } catch (_) { throw new Error('Connection interrupted. Retry this same attempt; do not start another payment.'); }
      if (!response.ok) {
        const messages = {
          cancellation_unconfirmed_check_original_payment:'The provider has not confirmed cancellation yet.',
          checkout_requires_reconciliation:'This earlier checkout needs staff review before another payment.',
          checkout_hold_expired_requires_review:'The hold has ended. Cancel or check the original checkout before paying again.',
          payment_cancellation_not_verified:'Cancellation has not been verified. Check the original checkout.',
          payment_cancellation_state_conflict:'This checkout has changed. Check its current status.',
          payment_snapshot_mismatch:'The payment details need staff review.',
          checkout_not_found:'This checkout could not be verified. Keep the original page and contact the test operator.'
        };
        throw new Error(messages[body.detail] || (typeof body.detail === 'string' ? body.detail : 'Check the test booking details.'));
      }
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
      if (!updateHold(checkout)) return false;
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
            attempt = {checkout_id:context.id, property_id:propertyId, check_in:values.check_in, check_out:values.check_out,
              guests:Number(values.guests), guest:{name:values.name, email:values.email, language:values.language}, quote_token:quote.quote_token};
            fields.disabled = true; // Freeze the exact payload and UUID across ambiguous network retries.
          }
          saveContext();
          checkout = await request('/api/public/booking', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(attempt)});
          await acceptCheckout();
        }
      } catch (error) {
        if (checkout) { pay.hidden = false; check.hidden = false; }
        if (attempt) cancel.hidden = false;
        next.textContent = attempt ? 'Retry same checkout' : 'Get quote'; tell(error.message);
      } finally { busy = false; fields.disabled = Boolean(attempt); next.disabled = Boolean(checkout); }
    });
    const poll = async () => {
      for (let i=0; i<30; i++) {
        const result = await request('/api/public/checkout/' + encodeURIComponent(context.id), {headers:{Authorization:'Bearer '+context.token}});
        if (finish(result)) return;
        updateHold(result);
        if (['refund_pending','refund_failed'].includes(result.state)) {
          pay.hidden = true; tell('No booking was created. Refund is not complete; the test operator must review it.'); return;
        }
        await new Promise(resolve => setTimeout(resolve, 2000));
      }
      tell('Confirmation is still pending. Use Check booking status or contact the test operator. Do not pay again.');
    };
    const acceptCheckout = async () => {
      if (finish(checkout)) return;
      check.hidden = false; resume.hidden = true; cancel.hidden = false;
      if (['refund_pending','refund_failed'].includes(checkout.state)) {
        cancel.hidden = true; pay.hidden = true; tell('Refund is not complete; use Check booking status or ask the test operator.'); return;
      }
      if (['succeeded','processing'].includes(checkout.provider_state)) {
        submitted = true; cancel.hidden = true; pay.hidden = true; tell('Existing payment submitted. Checking its original booking…'); await poll(); return;
      }
      if (checkout.cancellation_available === false) {
        pay.hidden = true; cancel.hidden = true; tell('The provider cannot confirm an unpaid cancellation. Check the original payment status; do not start another payment.'); return;
      }
      if (checkout.cancellation_pending_review) {
        pay.hidden = true; cancel.textContent = 'Retry cancellation';
        tell('A previous cancellation is not confirmed. Retry cancelling this checkout or check its status; do not start another payment.'); return;
      }
      if (checkout.provider_state === 'canceled') {
        pay.hidden = true; tell('This payment was cancelled. Ask the test operator to review it before starting again.'); return;
      }
      mountPayment();
    };
    resume.addEventListener('click', async () => {
      if (busy || terminal) return;
      busy = true; resume.disabled = true;
      try {
        checkout = await request('/api/public/checkout/' + encodeURIComponent(context.id) + '/resume',
          {method:'POST',headers:{Authorization:'Bearer '+context.token}});
        await acceptCheckout();
      } catch (error) { tell(error.message + ' Keep this checkout identity; do not start another payment.'); }
      finally { busy = false; resume.disabled = false; }
    });
    cancel.addEventListener('click', async () => {
      if (busy || terminal) return;
      busy = true; cancel.disabled = true; pay.disabled = true; check.disabled = true;
      tell('Checking whether the original test payment can be cancelled…');
      try {
        checkout = await request('/api/public/checkout/' + encodeURIComponent(context.id) + '/cancel',
          {method:'POST',headers:{Authorization:'Bearer '+context.token}});
        await acceptCheckout();
      } catch (error) {
        pay.hidden = true; resume.hidden = false; cancel.textContent = 'Retry cancellation';
        tell(error.message + ' Retry this cancellation or check the original payment; do not start another.');
      } finally { busy = false; cancel.disabled = false; check.disabled = false; pay.disabled = submitted || holdDeadline <= performance.now(); }
    });
    fresh.addEventListener('click', () => {
      if (!terminal) return;
      sessionStorage.removeItem(storageKey); window.location.reload();
    });
    pay.addEventListener('click', async () => {
      if (busy || !checkout || submitted || terminal || holdDeadline <= performance.now()) return;
      busy = true; cancel.disabled = true; pay.disabled = true; check.disabled = true;
      try {
        const current = await request('/api/public/checkout/' + encodeURIComponent(context.id), {headers:{Authorization:'Bearer '+context.token}});
        if (finish(current)) return;
        if (current.cancellation_pending_review) { pay.hidden = true; cancel.hidden = false; cancel.textContent = 'Retry cancellation'; tell('Cancellation is not confirmed. Retry cancelling or check the original payment.'); return; }
        if (!updateHold(current)) { tell('The inventory hold has ended. Cancel or check this original checkout before starting another.'); return; }
        if (!elements) mountPayment();
        const result = await stripe.confirmPayment({elements, redirect:'if_required'});
        if (result.error) throw new Error(result.error.message);
        submitted = true; cancel.hidden = true; pay.hidden = true;
        tell('Payment submitted. Waiting for the signed callback…'); await poll();
      } catch (error) { tell(error.message); }
      finally { busy = false; cancel.disabled = false; pay.disabled = submitted; check.disabled = false; }
    });
    check.addEventListener('click', async () => {
      if (busy || !checkout || terminal) return;
      busy = true; check.disabled = true; pay.disabled = true;
      try { await poll(); } catch (error) { tell(error.message); }
      finally { busy = false; check.disabled = false; pay.disabled = submitted; }
    });
    try {
      const saved = sessionStorage.getItem(storageKey);
      if (saved) {
        const previous = JSON.parse(saved);
        if (previous.propertyId !== propertyId || typeof previous.id !== 'string' || typeof previous.token !== 'string'
            || !Number.isFinite(previous.at) || Date.now()-previous.at > 24*60*60*1000) {
          throw new Error('Saved checkout needs an operator review; no new payment will be started.');
        }
        context = previous; fields.disabled = true; next.disabled = true; resume.hidden = false; cancel.hidden = false;
        tell('A checkout is saved in this tab. Resume it to recover the original payment; no guest or card details were stored.');
      }
    } catch (error) {
      fields.disabled = true; next.disabled = true; tell(error.message);
    }
  });
})();
