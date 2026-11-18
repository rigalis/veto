const sidebar = document.getElementById("sidebar");
const menuToggle = document.getElementById("menu-toggle");
const guideLinks = [...document.querySelectorAll("[data-doc-nav]")];

function selectGuideSection(id) {
  guideLinks.forEach((link) => link.classList.toggle("active", link.dataset.docNav === id));
}

menuToggle.addEventListener("click", () => {
  const open = sidebar.classList.toggle("open");
  menuToggle.setAttribute("aria-expanded", String(open));
});

guideLinks.forEach((link) => {
  link.addEventListener("click", () => {
    selectGuideSection(link.dataset.docNav);
    sidebar.classList.remove("open");
    menuToggle.setAttribute("aria-expanded", "false");
  });
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && sidebar.classList.contains("open")) {
    sidebar.classList.remove("open");
    menuToggle.setAttribute("aria-expanded", "false");
    menuToggle.focus();
  }
});

const observer = new IntersectionObserver((entries) => {
  const visible = entries.filter((entry) => entry.isIntersecting)
    .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
  if (visible.length) selectGuideSection(visible[0].target.id);
}, { rootMargin: "-85px 0px -70% 0px" });

document.querySelectorAll(".docs-content [id]").forEach((section) => observer.observe(section));
selectGuideSection(location.hash.slice(1) || "overview");
