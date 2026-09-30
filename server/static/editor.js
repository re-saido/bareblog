// Uploads images dropped, pasted or picked in the editor via PUT /files/<key>
// (the login cookie authenticates it) and inserts markdown for them at the cursor.
// If-None-Match: * stops an upload replacing a file; a taken name gets -2, -3...
const form = document.querySelector(".editor");
const text = form.querySelector("textarea");
const status = document.getElementById("upload-status");
const MAX_BYTES = Number(form.dataset.maxBytes);
const EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"};

async function upload(file) {
  const ext = EXT[file.type];
  if (!ext) throw new Error(`${file.name}: only PNG, JPEG, GIF and WebP images`);
  if (file.size > MAX_BYTES) throw new Error(`${file.name}: over ${MAX_BYTES / 1e6} MB`);
  const name = file.name.replace(/\.[^.]*$/, "");
  const stem = name.normalize("NFKD").toLowerCase().replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "").slice(0, 80) || "image";
  for (let n = 1; n <= 100; n++) {
    const key = `${n > 1 ? `${stem}-${n}` : stem}.${ext}`;
    const resp = await fetch(`/files/${key}`, {method: "PUT", body: file, headers: {"If-None-Match": "*"}});
    if (resp.status === 412) continue;
    if (!resp.ok) throw new Error(`${file.name}: ${(await resp.text()).trim()}`);
    return `![${name.replace(/[\[\]]/g, "")}](/files/${key})`;
  }
  throw new Error(`${file.name}: too many files called ${stem}`);
}

async function add(files) {
  const errors = [];
  for (const file of files) {
    status.className = "";
    status.textContent = `Uploading ${file.name}…`;
    try {
      text.setRangeText(await upload(file) + "\n", text.selectionStart, text.selectionEnd, "end");
    } catch (e) {
      errors.push(e.message);
    }
  }
  status.className = errors.length ? "error" : "";
  status.textContent = errors.join("\n");
  text.focus();
}

document.getElementById("image").addEventListener("change", e => { add([...e.target.files]); e.target.value = ""; });
text.addEventListener("dragover", e => { if (e.dataTransfer.types.includes("Files")) { e.preventDefault(); text.classList.add("dropping"); } });
text.addEventListener("dragleave", () => text.classList.remove("dropping"));
text.addEventListener("drop", e => {
  text.classList.remove("dropping");
  if (!e.dataTransfer.files.length) return;  // dropped text: let the browser insert it
  e.preventDefault();
  add([...e.dataTransfer.files]);
});
text.addEventListener("paste", e => {
  const files = [...e.clipboardData.files];
  if (files.length) { e.preventDefault(); add(files); }
});
