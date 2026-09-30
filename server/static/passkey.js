// Passkey buttons for the login and passkeys pages. The browser's own JSON
// helpers turn the server's WebAuthn options into a passkey prompt and the
// answer back again.
const ORIGIN = document.currentScript.dataset.origin;

async function postJSON(url, body) {
  const resp = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body ?? {})});
  if (!resp.ok) throw new Error((await resp.text()).trim() || resp.statusText);
  return resp.json();
}

function passkeyButton(id, run) {
  const status = document.getElementById("passkey-status");
  document.getElementById(id)?.addEventListener("click", async e => {
    e.preventDefault();
    status.textContent = "";
    try {
      if (location.origin !== ORIGIN) throw new Error(`Passkeys only work at ${ORIGIN}`);
      if (!window.PublicKeyCredential?.parseCreationOptionsFromJSON) throw new Error("This browser is too old for passkeys");
      await run();
    } catch (err) {
      status.textContent = err.name === "NotAllowedError" ? "Cancelled." : err.message;
    }
  });
}

passkeyButton("passkey-login", async () => {
  const options = PublicKeyCredential.parseRequestOptionsFromJSON(await postJSON("/login/passkey/options"));
  await postJSON("/login/passkey", (await navigator.credentials.get({publicKey: options})).toJSON());
  location = "/";
});

passkeyButton("add-passkey", async () => {
  const options = PublicKeyCredential.parseCreationOptionsFromJSON(await postJSON("/passkeys/options"));
  const credential = (await navigator.credentials.create({publicKey: options})).toJSON();
  await postJSON("/passkeys/add", {name: document.getElementById("passkey-name").value, credential});
  location.reload();
});
