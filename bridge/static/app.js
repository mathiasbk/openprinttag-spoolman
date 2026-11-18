// On a phone the menu folds away behind a button. Without JavaScript it stays open.
document.querySelectorAll("[data-menu-toggle]").forEach((toggle) => {
  const nav = document.getElementById(toggle.getAttribute("aria-controls"));
  const narrow = window.matchMedia("(max-width: 48rem)");
  const fold = () => {
    toggle.hidden = !narrow.matches;
    nav.classList.toggle("collapsed", narrow.matches && toggle.getAttribute("aria-expanded") !== "true");
  };
  toggle.addEventListener("click", () => {
    toggle.setAttribute("aria-expanded", String(toggle.getAttribute("aria-expanded") !== "true"));
    fold();
  });
  narrow.addEventListener("change", fold);
  fold();
});

// navigator.clipboard needs HTTPS, and this page usually runs on plain HTTP, so it falls back to a
// selected text field. iOS Safari only copies from one that is editable and selected with setSelectionRange.
const copyText = async (text) => {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Fall through to the old way.
    }
  }
  const field = document.createElement("textarea");
  field.value = text;
  field.setAttribute("readonly", "");
  field.style.position = "fixed";
  field.style.top = "0";
  field.style.opacity = "0";
  field.style.fontSize = "16px";
  document.body.append(field);
  field.contentEditable = "true";
  field.focus();
  field.select();
  field.setSelectionRange(0, text.length);
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch {
    copied = false;
  }
  field.remove();
  return copied;
};

document.querySelectorAll("[data-copy]").forEach((button) => {
  button.addEventListener("click", async () => {
    const source = document.getElementById(button.dataset.copy);
    if (await copyText(source.textContent)) {
      button.textContent = "Copied";
      return;
    }
    // Select the code itself, so a long press copies it.
    const range = document.createRange();
    range.selectNodeContents(source);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    button.textContent = "Selected. Copy it from the menu";
  });
});

// Options are removed rather than hidden, because Safari ignores hidden on an option.
document.querySelectorAll("[data-spool-filter]").forEach((search) => {
  const select = search.nextElementSibling;
  const radio = search.closest("label").querySelector("input[type=radio]");
  const [placeholder, ...spools] = select.options;
  const chooseAnother = () => { radio.checked = true; };

  search.hidden = false;
  search.addEventListener("input", () => {
    const words = search.value.toLowerCase().split(/\s+/).filter(Boolean);
    const chosen = select.value;
    const matches = spools.filter((option) => {
      const text = option.dataset.search.toLowerCase();
      return words.every((word) => text.includes(word));
    });
    select.replaceChildren(placeholder, ...matches);
    select.value = words.length && matches.length === 1 ? matches[0].value : chosen;
    if (select.selectedIndex < 0) select.value = "";
    chooseAnother();
  });
  // Enter would otherwise submit the form before a spool is chosen.
  search.addEventListener("keydown", (event) => {
    if (event.key === "Enter") event.preventDefault();
  });
  select.addEventListener("change", chooseAnother);
});

// The warehouse page: search, filter, sort and open link forms. Without JavaScript every spool and form is listed.
document.querySelectorAll("[data-inventory]").forEach((body) => {
  // Each spool row, with the link form row that follows it, if any.
  const pairs = [...body.querySelectorAll(":scope > .inv-row")].map((row) => {
    const next = row.nextElementSibling;
    return { row, form: next && next.matches("[data-form-for]") ? next : null };
  });
  const search = document.querySelector("[data-inventory-search]");
  const segments = [...document.querySelectorAll("[data-show]")];
  const chips = [...document.querySelectorAll(".chip[data-material]")];
  const heads = [...document.querySelectorAll(".sort-head")];
  const none = document.querySelector("[data-inventory-none]");
  const materials = new Set();
  const open = new Set();
  let show = "";
  let sortKey = "";
  let ascending = true;

  // Unknown weights are -1 and go last either way.
  const number = (value) => (Number(value) < 0 ? null : Number(value));
  const compare = (a, b) => {
    const x = a.row.dataset[sortKey];
    const y = b.row.dataset[sortKey];
    if (["id", "left", "used", "spoolman"].includes(sortKey)) {
      const [m, n] = [number(x), number(y)];
      if (m === null || n === null) return (m === null) - (n === null);
      return ascending ? m - n : n - m;
    }
    return ascending ? x.localeCompare(y) : y.localeCompare(x);
  };

  const update = () => {
    const words = search.value.toLowerCase().split(/\s+/).filter(Boolean);
    let shown = 0;
    pairs.forEach(({ row, form }) => {
      const text = row.dataset.search.toLowerCase();
      const visible = (!show || row.dataset.state.split(" ").includes(show))
        && (!materials.size || materials.has(row.dataset.material))
        && words.every((word) => text.includes(word));
      row.hidden = !visible;
      if (form) form.hidden = !visible || !open.has(form.id);
      if (visible) shown += 1;
    });
    none.hidden = shown > 0;
  };

  document.querySelectorAll("[data-inventory-tools]").forEach((tools) => { tools.hidden = false; });
  search.addEventListener("input", update);
  segments.forEach((button) => button.addEventListener("click", () => {
    show = button.dataset.show;
    segments.forEach((other) => other.setAttribute("aria-pressed", String(other === button)));
    update();
  }));
  chips.forEach((chip) => chip.addEventListener("click", () => {
    const on = chip.getAttribute("aria-pressed") !== "true";
    chip.setAttribute("aria-pressed", String(on));
    if (on) materials.add(chip.dataset.material); else materials.delete(chip.dataset.material);
    update();
  }));
  heads.forEach((head) => head.addEventListener("click", () => {
    ascending = sortKey === head.dataset.sort ? !ascending : true;
    sortKey = head.dataset.sort;
    heads.forEach((other) => {
      const th = other.closest("th");
      if (other === head) th.setAttribute("aria-sort", ascending ? "ascending" : "descending");
      else th.removeAttribute("aria-sort");
    });
    body.replaceChildren(...[...pairs].sort(compare).flatMap(({ row, form }) => (form ? [row, form] : [row])));
  }));
  body.querySelectorAll("[data-link-toggle]").forEach((toggle) => {
    toggle.addEventListener("click", (event) => {
      event.preventDefault();
      const id = toggle.dataset.linkToggle;
      if (open.has(id)) open.delete(id); else open.add(id);
      toggle.setAttribute("aria-expanded", String(open.has(id)));
      update();
    });
  });
  update();
});

// The Connect setup step waits for the bookmark and moves on once the login has arrived.
document.querySelectorAll("[data-login-wait]").forEach((notice) => {
  const loggedIn = notice.hasAttribute("data-logged-in");
  // Someone who is already logged in may only be looking, so the wait stays quiet until something happens.
  notice.hidden = loggedIn;
  const show = (kind, text) => {
    notice.hidden = false;
    notice.classList.remove("ok", "error");
    if (kind) notice.classList.add(kind);
    notice.textContent = text;
  };
  const check = async () => {
    let result;
    try {
      const response = await fetch(notice.dataset.loginWait, { headers: { Accept: "application/json" } });
      if (response.redirected || !response.ok) {
        // The admin session has expired, so the answer is the login page.
        show("error", "");
        notice.append("You were logged out of the bridge. ");
        const link = document.createElement("a");
        link.href = notice.dataset.loginPage;
        link.textContent = "Log in again";
        notice.append(link, ", then reload this page.");
        return;
      }
      result = await response.json();
    } catch {
      // The bridge may be restarting; keep waiting.
      setTimeout(check, 2000);
      return;
    }
    if (result.state === "delivered") {
      show("ok", `${result.message} Continuing…`);
      setTimeout(() => { window.location.href = notice.dataset.loginNext; }, 1500);
    } else if (result.state === "warning") {
      // Important enough that the page must not move on before it is read.
      show("error", `${result.message} `);
      const link = document.createElement("a");
      link.href = notice.dataset.loginNext;
      link.textContent = "Continue anyway";
      notice.append(link);
    } else if (result.state === "failed") {
      show("error", `${result.message} Reload this page to get a new bookmark and try again.`);
    } else if (result.state === "expired") {
      if (loggedIn) notice.hidden = true;
      else show("error", "This bookmark has expired. Reload the page to get a new one.");
    } else {
      setTimeout(check, 2000);
    }
  };
  setTimeout(check, 2000);
});
