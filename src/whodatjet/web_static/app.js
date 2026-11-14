(function () {
  "use strict";
  var form = document.getElementById("search-form");
  var qIn = document.getElementById("q"), dIn = document.getElementById("d");
  var go = document.getElementById("go"), again = document.getElementById("again");
  var err = document.getElementById("form-error");
  var result = document.querySelector(".result");
  var label = document.getElementById("result-label");
  var card = document.getElementById("card"), nf = document.getElementById("notfound"), loading = document.getElementById("loading");

  function $(id) { return document.getElementById(id); }
  var lastView = null;  // "card" or "nf": what to go back to if a search is rejected
  function show(which) {
    if (which === "card" || which === "nf") lastView = which;
    card.hidden = which !== "card"; nf.hidden = which !== "nf"; loading.hidden = which !== "loading";
    result.setAttribute("aria-busy", which === "loading" ? "true" : "false");
  }
  function setBusy(b) { go.disabled = b; again.disabled = b; }

  function render(data, kind) {
    err.hidden = true;
    if (!data.found) {
      label.textContent = kind === "random" ? "Random aircraft" : "Search result";
      $("nf-nnum").textContent = data.n_number || "";
      $("nf-message").textContent = data.message || "No registration found.";
      $("nf-coverage").textContent = data.coverage_start
        ? "WhoDatJet holds registry data from " + data.coverage_start + " to " + data.coverage_end + "." : "";
      show("nf"); return;
    }
    label.textContent = kind === "random" ? "Random aircraft" : "Search result";
    $("nnum").textContent = data.n_number;
    $("headline").textContent = data.headline;
    var dl = $("facts"); dl.textContent = "";
    data.facts.forEach(function (f) {
      var row = document.createElement("div"), dt = document.createElement("dt"), dd = document.createElement("dd");
      dt.textContent = f.label; dd.textContent = f.value; row.appendChild(dt); row.appendChild(dd); dl.appendChild(row);
    });
    var m = data.meta || {}, parts = [];
    if (m.as_of) parts.push("As of " + m.as_of);
    if (m.observed) parts.push("FAA data from " + m.observed);
    if (m.confidence) parts.push("confidence: " + m.confidence);
    if (m.flags && m.flags.length) parts.push(m.flags.join(", ").replace(/_/g, " "));
    $("meta").textContent = parts.join(" · ");
    show("card");
  }

  function fail(msg) {
    label.textContent = "Something went wrong";
    $("nf-nnum").textContent = ""; $("nf-message").textContent = msg; $("nf-coverage").textContent = "";
    show("nf");
  }

  function request(url, kind) {
    show("loading"); setBusy(true);
    return fetch(url, { headers: { Accept: "application/json" } })
      .then(function (r) {
        return r.json().then(function (body) { return { ok: r.ok, status: r.status, body: body }; });
      })
      .then(function (r) {
        if (!r.ok) {
          if (r.status === 400) {
            err.textContent = r.body.detail || "That search is not valid."; err.hidden = false;
            if (lastView) show(lastView); else fail(err.textContent);
            return;
          }
          fail("The server answered with an error (" + r.status + "). Please try again."); return;
        }
        render(r.body, kind);
      })
      .catch(function () { fail("Could not reach the server. Check your connection and try again."); })
      .then(function () { setBusy(false); });
  }

  function search(n, d) {
    var url = "/ui/search?q=" + encodeURIComponent(n) + (d ? "&date=" + encodeURIComponent(d) : "");
    var qs = "?n=" + encodeURIComponent(n) + (d ? "&date=" + encodeURIComponent(d) : "");
    history.replaceState(null, "", qs);
    return request(url, "search");
  }
  function random() {
    history.replaceState(null, "", location.pathname);
    return request("/ui/random", "random");
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var n = qIn.value.trim();
    if (!n) { err.textContent = "Enter an N-number, for example N172SP."; err.hidden = false; qIn.focus(); return; }
    search(n, dIn.value);
  });
  again.addEventListener("click", function () { err.hidden = true; random(); });

  var params = new URLSearchParams(location.search);
  if (params.get("n")) {
    qIn.value = params.get("n"); if (params.get("date")) dIn.value = params.get("date");
    search(qIn.value, dIn.value);
  } else {
    random();
  }
})();
