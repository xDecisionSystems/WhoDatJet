document.addEventListener("click", function (e) {
  var b = e.target.closest("[data-copy]");
  if (!b) return;
  var el = document.getElementById(b.getAttribute("data-copy"));
  if (el && navigator.clipboard) navigator.clipboard.writeText(el.textContent.trim()).then(function () { b.textContent = "Copied"; });
});
