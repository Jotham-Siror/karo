(function () {
  var chat = document.getElementById("chat");

  // A new id on every message, so the server can tell a retry of a message from a new message that reads the same.
  function newId() {
    return (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2);
  }

  // Deep Chat shows a bare URL as plain text. A markdown link makes the PIN page one tap on a phone.
  // Only Karo's own PIN and pay pages get one: any other URL may come from an invoice's text, so it stays plain.
  // The links are built from PUBLIC_BASE_URL, and this page is served from that same address.
  function withLinks(text) {
    var own = window.location.origin;
    return text.replace(/https?:\/\/[^\s)]+/g, function (url) {
      var ours = url.indexOf(own + "/confirm/") === 0 || url.indexOf(own + "/standin/pay/") === 0;
      return ours ? "[Open the link](" + url + ")" : url;
    });
  }

  function startInbox() {
    function poll() {
      fetch(chat.dataset.inboxUrl, { credentials: "same-origin" })
        .then(function (response) {
          if (response.status === 401) { window.location.href = "/"; return []; }
          return response.ok ? response.json() : [];
        })
        .then(function (messages) {
          // The second argument makes Deep Chat treat it as a new message, which is what saves it to the browser.
          messages.forEach(function (m) { chat.addMessage({ text: withLinks(m.text), role: "ai" }, true); });
        })
        // A missed poll is harmless: the messages stay waiting on the server and the next poll fetches them.
        .catch(function () {});
    }
    poll();
    setInterval(poll, 3000);
  }

  customElements.whenDefined("deep-chat").then(function () {
    chat.requestInterceptor = function (details) {
      if (details.body instanceof FormData) {
        details.body.append("message_id", newId());
      } else {
        details.body.message_id = newId();
      }
      return details;
    };
    chat.onComponentRender = startInbox;
  });
})();
