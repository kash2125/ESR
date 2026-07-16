(function () {
  const savedTheme = localStorage.getItem("engineeringDashboard.theme");
  document.documentElement.dataset.theme = savedTheme === "dark" ? "dark" : "light";
})();
