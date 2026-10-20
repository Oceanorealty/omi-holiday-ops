/* B5 sandbox checkout widget. Card details go only to Stripe.js. */
(() => {
  "use strict";
  document.querySelectorAll("[data-omi-checkout]").forEach((root) => {
    const api = new URL(root.dataset.apiBase || location.origin);
    if (api.protocol !== "https:" && !["localhost", "127.0.0.1"].includes(api.hostname)) return;
    const propertyId = Number(root.dataset.propertyId);
    const pk = root.dataset.stripeKey || "";
    root.innerHTML = `<form>
      <p><strong>Test booking · AUD · no real payment</strong></p>
      <label>Check-in <input name="check_in" type="date" required></label>
      <label>Check-out <input name="check_out" type="date" required></label>
      <label>Guests <input name="guests" type="number" min="1" max="100" value="1" required></label>
      <label>Name <input name="name" maxlength="200" autocomplete="name" required></label>
      <label>Email <input name="email" type="email" maxlength="254" autocomplete="email" required></label>
      <label>Language <select name="language"><option value="en">English</option><option value="zh">中文</option></select></label>
      <button type="submit">Get quote</button>
    </form><p role="status" aria-live="polite"></p><div data-payment></div>
    <button type="button" data-pay hidden>Pay with test card</button>`;
    const form = root.querySelector("form");
    const status = root.querySelector('[role="status"]');
    const pay = root.querySelector("[data-pay]");
    const quoteButton = form.querySelector("button");
    let step = "quote", quote, stripe, elements, checkout, pending = false;
    const tell = (message) => { status.textContent = message; };
    const endpoint = (path) => new URL(path, api).href;
    const request = async (path, options = {}) => {
      const response = await fetch(endpoint(path), {credentials: "omit", cache: "no-store", ...options});
      const body = await response.json();
      if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Please check the booking details.");
      return body.result;
    };
    const reset = () => {
      if (pending || checkout) return;
      delete root.dataset.checkoutId;
      quote = undefined; step = "quote"; quoteButton.textContent = "Get quote";
    };
    form.addEventListener("input", reset);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (pending || checkout) return;
      if (!Number.isInteger(propertyId) || propertyId < 1 || !/^pk_test_/.test(pk) || !window.Stripe) {
        tell("Configure a test property, Stripe test publishable key and Stripe.js first."); return;
      }
      pending = true; quoteButton.disabled = true;
      try {
        const values = Object.fromEntries(new FormData(form));
        if (step === "quote") {
          const params = new URLSearchParams({property_id: propertyId, check_in: values.check_in,
            check_out: values.check_out, guests: values.guests});
          quote = await request("/api/public/quote?" + params);
          if (!quote.bookable) throw new Error(quote.reason || "This stay is unavailable.");
          step = "checkout"; quoteButton.textContent = "Continue to test payment";
          tell(`Total AUD ${quote.total}. Continue to enter test card details.`);
        } else {
          // Keep the ID across network retries, but never persist card data.
          root.dataset.checkoutId ||= crypto.randomUUID();
          checkout = await request("/api/public/booking", {method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({checkout_id: root.dataset.checkoutId, property_id: propertyId,
              check_in: values.check_in, check_out: values.check_out, guests: Number(values.guests),
              guest: {name: values.name, email: values.email, language: values.language},
              quote_token: quote.quote_token})});
          stripe = window.Stripe(pk);
          elements = stripe.elements({clientSecret: checkout.client_secret});
          elements.create("payment").mount(root.querySelector("[data-payment]"));
          for (const input of form.elements) input.disabled = true;
          pay.hidden = false;
          tell(`Test payment: AUD ${checkout.amount}. Use a Stripe sandbox card.`);
        }
      } catch (error) {
        if (error.message === "quote_expired_or_changed") {
          step = "quote"; quoteButton.textContent = "Get quote";
        }
        tell(error.message);
      }
      finally { pending = false; quoteButton.disabled = Boolean(checkout); }
    });
    pay.addEventListener("click", async () => {
      if (pending || !checkout) return;
      pending = true; pay.disabled = true;
      try {
        const result = await stripe.confirmPayment({elements, redirect: "if_required"});
        if (result.error) throw new Error(result.error.message);
        tell("Payment submitted. Waiting for booking confirmation…");
        for (let i = 0; i < 30; i++) {
          const state = await request("/api/public/booking/" + encodeURIComponent(checkout.payment_intent_id),
            {headers: {Authorization: "Bearer " + checkout.access_token}});
          if (state.state === "confirmed") { tell(`Test booking confirmed. Reference: ${state.booking_ref}`); pay.hidden = true; return; }
          if (state.state === "refunded") { tell("The stay became unavailable; the test payment was refunded."); pay.hidden = true; return; }
          if (["refund_pending", "refund_failed"].includes(state.state)) {
            tell("The booking was not created. A refund requires follow-up; contact the test operator."); pay.hidden = true; return;
          }
          await new Promise(resolve => setTimeout(resolve, 2000));
        }
        tell("Confirmation is still pending. Contact the test operator before trying another payment.");
        pay.hidden = true;
      } catch (error) { tell(error.message); }
      finally { pending = false; pay.disabled = false; }
    });
  });
})();
