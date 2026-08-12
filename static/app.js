const input = document.getElementById("input");
const send = document.getElementById("send");
const messages = document.getElementById("messages");
const typing = document.getElementById("typing");

let vehicleState = {};

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"
  }[c]));
}

function addUser(text) {
  const el = document.createElement("div");
  el.className = "message user";
  el.innerHTML = `<div class="bubble">${escapeHtml(text)}</div>`;
  messages.appendChild(el);
  scrollBottom();
}

function addAssistant(html) {
  const el = document.createElement("div");
  el.className = "message assistant";
  el.innerHTML = `<div class="avatar">K</div><div class="bubble">${html}</div>`;
  messages.appendChild(el);
  scrollBottom();
}

function money(value) {
  return new Intl.NumberFormat("tr-TR", {
    style:"currency", currency:"TRY", maximumFractionDigits:0
  }).format(value);
}

function optionButtons(options) {
  if (!options || !options.length) return "";
  return `<div class="quick-options">${
    options.map(o => `
      <button class="choice quick-choice" data-answer="${encodeURIComponent(o)}">
        ${escapeHtml(o)}
      </button>
    `).join("")
  }</div>`;
}

function renderResult(data) {
  if (data.status === "found") {
    return `
      Aracınızı buldum.
      <div class="result">
        <div class="result-label">${data.calculated ? "Hesaplanan kasko değeri" : "Kasko değer listesi sonucu"}</div>
        <div class="result-value">${money(data.value)}</div>
        <div class="result-car">${escapeHtml(data.year)} ${escapeHtml(data.brand)} — ${escapeHtml(data.type)}</div>
        <div class="kasko-code">
          <span class="kasko-code-label">KASKO KODU</span>
          <span class="kasko-code-value">${escapeHtml(data.kasko_code || "")}</span>
        </div>
        ${data.calculated ? `
          <div class="calculation-note">
            Bu model yılı güncel listede doğrudan yer almadığından,
            ${escapeHtml(data.base_year)} model için listede bulunan değer esas alınmış
            ve her model yılı için bir önceki yıl değeri üzerinden %10 indirim uygulanarak hesaplanmıştır.
          </div>` : ""}
      </div>
    `;
  }

  if (data.status === "clarify" || data.status === "need_info") {
    const count = data.candidate_count
      ? `<div style="margin-top:8px;color:#71849d;font-size:10px">${data.candidate_count} uygun kayıt arasından daraltıyorum.</div>`
      : "";
    return `${escapeHtml(data.question)}${optionButtons(data.options)}${count}`;
  }

  if (data.status === "not_found") {
    return escapeHtml(data.message || "Aracınıza uygun bir kasko değeri bulunamadı. Araç bilgilerini kontrol ederek tekrar deneyebilirsiniz.");
  }

  return "Sonucu netleştiremedim. Aracın marka, model, yıl, motor veya paket bilgisini biraz daha ayrıntılı yazar mısınız?";
}

async function search(customText = null) {
  const text = (customText ?? input.value).trim();
  if (!text) return;

  addUser(text);
  input.value = "";
  resizeInput();
  typing.classList.remove("hidden");
  send.disabled = true;
  scrollBottom();

  try {
    const res = await fetch("/api/search", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({message:text,state:vehicleState})
    });
    const data = await res.json();

    if (!res.ok || !data.ok) {
      addAssistant(`Bir sorun oluştu: <strong>${escapeHtml(data.error || "Bilinmeyen hata")}</strong>`);
    } else {
      vehicleState = data.state || vehicleState;
      addAssistant(renderResult(data));
    }
  } catch (err) {
    addAssistant("Sunucuya ulaşılamadı. Lütfen tekrar deneyin.");
  } finally {
    typing.classList.add("hidden");
    send.disabled = false;
    input.focus({preventScroll:true});
    scrollBottom();
  }
}

function resizeInput() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 112) + "px";
}

send.addEventListener("click", () => search());

input.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    search();
  }
});
input.addEventListener("input", resizeInput);

document.querySelectorAll(".examples button").forEach(btn => {
  btn.addEventListener("click", () => {
    input.value = btn.dataset.example;
    resizeInput();
    input.focus();
  });
});

messages.addEventListener("click", e => {
  const btn = e.target.closest(".quick-choice");
  if (!btn) return;
  search(decodeURIComponent(btn.dataset.answer));
});

// Mobil klavye açıldığında konuşmanın sonunu görünür tut.
if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", () => {
    setTimeout(scrollBottom, 80);
  });
}

function scrollBottom() {
  requestAnimationFrame(() => {
    messages.scrollTop = messages.scrollHeight;
  });
}
