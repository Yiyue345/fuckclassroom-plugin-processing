(() => {
  // The classroom PPT endpoint returns an attachment, not a PJAX HTML page.
  // The shell intercepts unmarked same-origin links and may replace the page
  // after this download click; never route it through partial navigation.
  const link = document.querySelector('[data-ppt-download]');
  if (!link) return;
  link.setAttribute('data-no-pjax', '');
  link.setAttribute('download', '');
})();
