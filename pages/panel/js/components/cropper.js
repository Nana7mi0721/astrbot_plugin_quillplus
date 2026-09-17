/* components/cropper.js — 由 index.html 拆分生成（可读性优先，逻辑与拆分前逐行一致） */
import { S } from "../state.js";
import { api } from "../api.js";
import { showToast } from "../components/feedback.js";
import { closeScrim, openScrim } from "../components/modal.js";
import { $ } from "../utils.js";

export { CROP_SIZE, applyCropTransform, clampCrop, closeCropDialog, generateCroppedDataUrl, handleAvatarFile, openCropDialog, recropAvatar, uploadAvatar };

/* ── 头像裁剪 ─────────────────────────────────────────────────────── */

const CROP_SIZE = 300;


function closeCropDialog() {
  closeScrim($("#cropScrim"));
  S._crop = null;
  const img = $("#cropImg");
  if (img) img.src = "";
}

function applyCropTransform() {
  if (!S._crop) return;
  const img = $("#cropImg");
  if (!img) return;
  img.style.width = (S._crop.natW * S._crop.scale) + "px";
  img.style.height = (S._crop.natH * S._crop.scale) + "px";
  img.style.left = S._crop.offsetX + "px";
  img.style.top = S._crop.offsetY + "px";
}

function clampCrop() {
  const w = S._crop.natW * S._crop.scale;
  const h = S._crop.natH * S._crop.scale;
  S._crop.offsetX = Math.min(0, Math.max(CROP_SIZE - w, S._crop.offsetX));
  S._crop.offsetY = Math.min(0, Math.max(CROP_SIZE - h, S._crop.offsetY));
}

/** 打开裁剪器。onConfirm(croppedDataUrl) / onSkip() 二选一。 */
function openCropDialog(dataUrl, onConfirm, onSkip) {
  const img = $("#cropImg");
  const stage = $("#cropStage");
  S._crop = null;

  img.onload = () => {
    const natW = img.naturalWidth || 1;
    const natH = img.naturalHeight || 1;
    const scale = Math.max(CROP_SIZE / natW, CROP_SIZE / natH);
    S._crop = {
      natW, natH, scale,
      minScale: scale,
      maxScale: scale * 3,
      offsetX: (CROP_SIZE - natW * scale) / 2,
      offsetY: (CROP_SIZE - natH * scale) / 2,
      dragging: false, lastX: 0, lastY: 0,
    };
    applyCropTransform();
    openScrim($("#cropScrim"));
  };
  img.src = dataUrl;

  $("#cropSkip").hidden = !onSkip;
  $("#cropOk").onclick = () => {
    const out = generateCroppedDataUrl();
    closeCropDialog();
    if (out && onConfirm) onConfirm(out);
  };
  $("#cropSkip").onclick = () => { closeCropDialog(); if (onSkip) onSkip(); };

  // 指针事件 + 捕获，拖出容器也能继续跟手（HIG：1:1 直接操作）
  stage.onpointerdown = (e) => {
    if (!S._crop) return;
    S._crop.dragging = true;
    S._crop.lastX = e.clientX; S._crop.lastY = e.clientY;
    stage.classList.add("is-dragging");
    stage.setPointerCapture(e.pointerId);
    e.preventDefault();
  };
  stage.onpointermove = (e) => {
    if (!S._crop || !S._crop.dragging) return;
    S._crop.offsetX += e.clientX - S._crop.lastX;
    S._crop.offsetY += e.clientY - S._crop.lastY;
    S._crop.lastX = e.clientX; S._crop.lastY = e.clientY;
    clampCrop();
    applyCropTransform();
  };
  const endDrag = (e) => {
    if (!S._crop) return;
    S._crop.dragging = false;
    stage.classList.remove("is-dragging");
    try { stage.releasePointerCapture(e.pointerId); } catch (_) { /* 指针已释放 */ }
  };
  stage.onpointerup = endDrag;
  stage.onpointercancel = endDrag;

  // 以指针为锚点缩放，保持指针下的像素不动
  stage.onwheel = (e) => {
    if (!S._crop) return;
    e.preventDefault();
    const rect = stage.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    const old = S._crop.scale;
    let next = old * (e.deltaY > 0 ? 0.9 : 1.1);
    next = Math.max(S._crop.minScale, Math.min(S._crop.maxScale, next));
    if (next === old) return;
    const px = (mx - S._crop.offsetX) / old;
    const py = (my - S._crop.offsetY) / old;
    S._crop.scale = next;
    S._crop.offsetX = mx - px * next;
    S._crop.offsetY = my - py * next;
    clampCrop();
    applyCropTransform();
  };
}

function generateCroppedDataUrl() {
  if (!S._crop) return null;
  const canvas = document.createElement("canvas");
  canvas.width = CROP_SIZE;
  canvas.height = CROP_SIZE;
  const ctx = canvas.getContext("2d");
  ctx.drawImage(
    $("#cropImg"),
    -S._crop.offsetX / S._crop.scale, -S._crop.offsetY / S._crop.scale,
    CROP_SIZE / S._crop.scale, CROP_SIZE / S._crop.scale,
    0, 0, CROP_SIZE, CROP_SIZE
  );
  return canvas.toDataURL("image/png");
}

async function handleAvatarFile(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  const ext = (file.name.split(".").pop() || "").toLowerCase();
  if (!["png", "jpg", "jpeg", "webp", "gif"].includes(ext)) { showToast("不支持的图片格式", "warning"); return; }
  if (file.size > 5 * 1024 * 1024) { showToast("图片过大（上限 5MB）", "warning"); return; }
  const dataUrl = await new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = (e) => resolve(e.target.result);
    r.onerror = () => reject(new Error("读取失败"));
    r.readAsDataURL(file);
  });
  S._originalAvatarDataUrl = dataUrl;
  openCropDialog(dataUrl,
    (cropped) => uploadAvatar(cropped, file.name),
    () => uploadAvatar(dataUrl, file.name)
  );
}

async function uploadAvatar(dataUrl, filename) {
  const ph = $("#avatarPlaceholder");
  if (ph) ph.innerHTML = '<div class="fs-13 t2">上传中…</div>';
  try {
    const b64 = String(dataUrl).split(",")[1] || "";
    const res = await api("/upload_avatar_base64", { method: "POST", body: JSON.stringify({ filename, b64_data: b64 }) });
    const pathField = $("#pf-avatar");
    if (pathField) pathField.value = res.path;
    const img = $("#avatarPreview");
    if (img) { img.src = dataUrl; img.hidden = false; }
    if (ph) ph.hidden = true;
    const recrop = $("#avatarRecrop");
    if (recrop) recrop.hidden = false;
    showToast("头像已上传", "success");
  } catch (e) {
    showToast("上传失败：" + e.message);
    if (ph) ph.hidden = false;
    if (ph) ph.innerHTML = '<svg class="icon icon--lg" aria-hidden="true"><use href="#i-camera"/></svg><div>点击上传头像</div>';
  }
}

async function recropAvatar() {
  const filename = ($("#pf-avatar")?.value || "").split("/").pop() || "avatar.png";
  if (S._originalAvatarDataUrl) {
    openCropDialog(S._originalAvatarDataUrl, (c) => uploadAvatar(c, filename), null);
    return;
  }
  const id = S._personaEditing && S._personaEditing.id;
  if (!id) { showToast("未找到角色卡 ID", "warning"); return; }
  try {
    const res = await api("/persona/avatar?id=" + encodeURIComponent(id));
    if (!res || !res.avatar_url) { showToast("该角色卡尚未设置头像", "warning"); return; }
    S._originalAvatarDataUrl = res.avatar_url;
    openCropDialog(res.avatar_url, (c) => uploadAvatar(c, filename), null);
  } catch (e) { showToast("加载头像失败：" + e.message); }
}

/* ══ I. 文档知识库 ═══════════════════════════════════════════════════ */
