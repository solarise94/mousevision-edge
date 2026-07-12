(function () {
  const params = new URLSearchParams(location.search);
  const intent = (params.get("intent") || "").toLowerCase();
  const to = (params.get("to") || "").toLowerCase();

  const ua = navigator.userAgent || "";
  const isMobile =
    /Android|iPhone|iPad|iPod|Mobile/i.test(ua) ||
    (navigator.maxTouchPoints > 1 && window.innerWidth < 900);

  function targetForIntent(kind) {
    if (kind === "record") return "/mobile";
    if (kind === "manage") return isMobile ? "/mobile/manage" : "/pc";
    return null;
  }

  function targetForTo(kind) {
    if (kind === "mobile") return "/mobile";
    if (kind === "manage") return isMobile ? "/mobile/manage" : "/pc";
    if (kind === "pc") return "/pc";
    return null;
  }

  function go(path) {
    const el = document.getElementById("redirecting");
    if (el) el.hidden = false;
    location.replace(path);
  }

  if (to) {
    const path = targetForTo(to);
    if (path) return go(path);
  }

  if (intent === "record" || intent === "manage") {
    return go(targetForIntent(intent));
  }

  const choices = document.getElementById("choices");
  if (choices) choices.hidden = false;

  const recBadge = document.getElementById("recBadge");
  const mgrBadge = document.getElementById("mgrBadge");
  if (isMobile) {
    if (recBadge) recBadge.hidden = false;
    document.getElementById("btnRecord")?.classList.add("recommended");
  } else {
    if (mgrBadge) mgrBadge.hidden = false;
    document.getElementById("btnManage")?.classList.add("recommended");
  }

  document.getElementById("btnRecord")?.addEventListener("click", () => go("/mobile"));
  document.getElementById("btnManage")?.addEventListener("click", () =>
    go(isMobile ? "/mobile/manage" : "/pc")
  );
})();
