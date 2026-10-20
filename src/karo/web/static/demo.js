// A form with data-confirm asks first. This is a file, not an onsubmit attribute, because the page policy blocks inline handlers.
document.addEventListener("submit", function (event) {
  var question = event.target.dataset.confirm;
  if (question && !confirm(question)) { event.preventDefault(); }
});
