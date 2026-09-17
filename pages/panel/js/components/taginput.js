/* components/taginput.js — 由 index.html 拆分生成（可读性优先，逻辑与拆分前逐行一致） */
import { $, esc } from "../utils.js";

export { createTagInput };

/* ── 标签输入（关键词编辑） ────────────────────────────────────────── */

function createTagInput(containerId, initial, placeholder) {
  const host = document.getElementById(containerId);
  if (!host) return null;
  host.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.className = "tagfield";
  const input = document.createElement("input");
  input.type = "text";
  input.placeholder = placeholder || "输入后回车或逗号添加";
  const tags = [...(initial || [])];

  function emit() { host.dispatchEvent(new CustomEvent("tagschange", { detail: [...tags] })); }

  function render() {
    wrap.innerHTML = "";
    tags.forEach((t, i) => {
      const chip = document.createElement("span");
      chip.className = "tag";
      chip.innerHTML = `${esc(t)}<span class="tag-del" data-idx="${i}" role="button" aria-label="移除">×</span>`;
      wrap.appendChild(chip);
    });
    wrap.appendChild(input);
    wrap.querySelectorAll(".tag-del").forEach(el => {
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        tags.splice(Number(el.dataset.idx), 1);
        render(); input.focus(); emit();
      });
    });
  }

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === "," || e.key === "，") {
      e.preventDefault();
      const v = input.value.trim().replace(/[,，]/g, "");
      if (v && !tags.includes(v)) { tags.push(v); render(); emit(); }
      input.value = "";
      input.focus();
    } else if (e.key === "Backspace" && !input.value && tags.length) {
      tags.pop(); render(); emit(); input.focus();
    }
  });
  wrap.addEventListener("click", (e) => { if (e.target === wrap) input.focus(); });
  host.appendChild(wrap);
  render();
  return { getTags: () => [...tags] };
}

/* ══ F. 写作素材库 ═══════════════════════════════════════════════════ */
