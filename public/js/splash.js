/* Har Mini App ochilishida (rol/ekrandan qat'iy nazar) bir marta ko'rinadigan
   brend animatsiyasi. index.html'da app.js'dan OLDIN yuklanadi va bootstrap()
   ishga tushishi bilan parallel o'ynaydi; ikkalasi tugagach (animatsiya HAM,
   birinchi ekran ham) splash yo'qoladi — shu sabab sekin tarmoqda ham
   qisqarib qolmaydi, tez tarmoqda ham ortiqcha kutmaydi. */
(function () {
  const el = document.getElementById("melores-splash");
  if (!el) return;
  const rotor = el.querySelector(".rotor");
  const ring = el.querySelector(".ring");
  const logoWrap = el.querySelector(".logo-wrap");

  rotor.classList.add("assemble");
  setTimeout(() => ring.classList.add("pulse"), 20);
  setTimeout(() => rotor.classList.add("fade-blades"), 20);
  setTimeout(() => logoWrap.classList.add("show"), 20);

  const animationDone = new Promise((resolve) => setTimeout(resolve, 1900));

  window.MeloresSplash = {
    ready: animationDone,
    hide() {
      el.classList.add("gone");
      setTimeout(() => el.remove(), 450);
    },
  };
})();
