"use strict";

// The phone page. It shows what the server answers and sends the reviewer's
// taps back. It decides nothing: every rule is enforced on the server.
//
// Text from a note or from the AI is always put on the page with textContent
// (or as a text node), which shows it as plain text. Nothing in this file
// builds HTML out of text, so a note cannot make the page run anything.

const LABELS = {
  "account.name": "Account",
  "contact.name": "Contact",
  "contact.email": "Email",
  "contact.phone": "Phone",
  "task.subject": "Follow-up",
  "task.due": "Due",
  "item": "Deal item",
  "commitment": "Promise made",
};
const KINDS = {customer_stated: "customer said", observed: "you noted", commitment: "you promised"};
const STATES = {pending: "to review", confirmed: "confirmed", rejected: "rejected", not_stated: "not stated"};

let view = null;        // {note, candidates}: what the server last sent
let products = [];      // [{id, name}] for the "pick a product" list
let selectedId = null;  // the suggestion whose words are highlighted in the note
let editingId = null;   // the suggestion being edited

function byId(id) {
  return document.getElementById(id);
}

// Make an element. text is shown as plain text.
function make(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function makeButton(text, className, onClick) {
  const button = make("button", className, text);
  button.type = "button";
  button.addEventListener("click", onClick);
  return button;
}

// Show a message in the bar at the bottom. An empty text hides the bar.
function say(text) {
  byId("message").textContent = text;
  byId("message").hidden = !text;
}

// --- Talking to the server ----------------------------------------------------------

// The one place that sends a request. It always says who is acting.
async function call(method, path, body) {
  const options = {method, headers: {"X-Acting-As": byId("actor").value}};
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  try {
    const response = await fetch(path, options);
    return {ok: response.ok, status: response.status, data: await response.json()};
  } catch (error) {
    return {ok: false, status: 0, data: {detail: "Could not reach the server."}};
  }
}

async function submitNote() {
  const text = byId("note-text").value;
  if (!text.trim()) {
    say("Write or dictate a note first.");
    return;
  }
  byId("submit").disabled = true;
  say("Reading the note...");
  const answer = await call("POST", "/notes", {text});
  byId("submit").disabled = false;

  if (answer.ok) {
    say("");
    show(answer.data);
  } else if (answer.status === 502) {
    // The note was saved but the AI failed. Show the note with "Try again".
    await openNote(answer.data.note_id);
    say(answer.data.detail);
  } else {
    say(answer.data.detail);
  }
}

async function openNote(noteId) {
  const answer = await call("GET", "/notes/" + noteId);
  if (answer.ok) show(answer.data);
  else say(answer.data.detail);
}

async function tryAgain() {
  byId("retry").disabled = true;
  say("Reading the note...");
  const answer = await call("POST", "/notes/" + view.note.id + "/extract");
  byId("retry").disabled = false;
  say(answer.ok ? "" : answer.data.detail);
  if (answer.ok) show(answer.data);
}

// Send one decision (confirm, reject or an edit), then show what the server
// now has. Returns nothing; a refusal is shown in the message bar.
async function decide(path, body) {
  const answer = await call("POST", path, body);
  if (!answer.ok) {
    say(answer.data.detail);
    return;
  }
  say("");
  editingId = null;
  await openNote(view.note.id);
}

async function confirmAll() {
  const answer = await call("POST", "/notes/" + view.note.id + "/confirm-all");
  if (!answer.ok) {
    say(answer.data.detail);
    return;
  }
  await openNote(view.note.id);
  const waiting = answer.data.waiting.length;
  say("Confirmed " + answer.data.confirmed.length + "."
      + (waiting ? " " + waiting + " still need their own confirm." : ""));
}

// --- Drawing ---------------------------------------------------------------------------

function show(data) {
  view = data;
  // Keep the note's number in the address, so reloading the page reopens it.
  history.replaceState(null, "", "#note=" + data.note.id);
  byId("write").hidden = true;
  byId("review").hidden = false;
  draw();
}

function newNote() {
  view = null;
  selectedId = null;
  editingId = null;
  history.replaceState(null, "", location.pathname);
  byId("note-text").value = "";
  byId("review").hidden = true;
  byId("write").hidden = false;
  say("");
}

function draw() {
  byId("review-title").textContent =
    "Note " + view.note.id + " by " + view.note.author + (view.note.locked ? " (locked)" : "");
  drawNote();
  byId("retry").hidden = view.candidates.length > 0 || view.note.locked;
  byId("confirm-all").hidden =
    view.note.locked || !view.candidates.some((c) => c.state === "pending");
  byId("candidates").replaceChildren(...view.candidates.map(drawCandidate));
}

// Show the note, with the words of the selected suggestion highlighted.
// The server counts positions in whole characters. JavaScript counts some
// characters (an emoji, for example) as two, so the text is first split into
// whole characters.
function drawNote() {
  const box = byId("note-view");
  const selected = view.candidates.find((c) => c.id === selectedId);
  if (!selected || !selected.span) {
    box.replaceChildren(view.note.text);
    return;
  }
  const characters = Array.from(view.note.text);
  const [start, end] = selected.span;
  box.replaceChildren(
    characters.slice(0, start).join(""),
    make("mark", "", characters.slice(start, end).join("")),
    characters.slice(end).join(""),
  );
}

// The words shown for a suggestion.
function describe(c) {
  if (c.type !== "item") return c.value === null ? "not stated" : c.value;
  if (c.product_id === null) return c.quantity + " x (no product picked)";
  const product = products.find((p) => p.id === c.product_id);
  return c.quantity + " x " + c.product_id + (product ? " - " + product.name : "");
}

function drawCandidate(c) {
  const row = make("li", "candidate " + c.state);
  if (c.id === selectedId) row.classList.add("selected");
  row.addEventListener("click", (event) => {
    // A tap on a button or an input inside the row is not a tap on the row.
    if (event.target.closest("button, input, select")) return;
    selectedId = c.id;
    draw();
  });

  row.append(make("div", "label", LABELS[c.name] || c.name));
  row.append(make("div", "value", describe(c)));

  const tags = make("div", "tags");
  tags.append(make("span", "tag", STATES[c.state] || c.state));
  if (c.kind) tags.append(make("span", "tag", KINDS[c.kind] || c.kind));
  if (c.edited) tags.append(make("span", "tag", "edited"));
  if (c.state === "pending" && c.needs_individual_confirmation) {
    tags.append(make("span", "tag warn", "needs its own confirm"));
  }
  row.append(tags);

  if (c.edited && c.original.value !== null) {
    row.append(make("div", "reason", "AI suggested: " + c.original.value));
  }
  if (!c.edited) {
    for (const reason of c.original.reasons) row.append(make("div", "reason", "Check: " + reason));
  }

  if (!view.note.locked) row.append(c.id === editingId ? drawEditor(c) : drawButtons(c));
  return row;
}

function drawButtons(c) {
  const box = make("div", "buttons");
  const path = "/candidates/" + c.id;
  if (c.state === "pending" || c.state === "rejected") {
    box.append(makeButton("Confirm", "main", () => decide(path + "/confirm")));
  }
  if (c.state === "pending" || c.state === "confirmed") {
    box.append(makeButton("Reject", "", () => decide(path + "/reject")));
  }
  box.append(makeButton(c.state === "not_stated" ? "Fill in" : "Edit", "", () => {
    editingId = c.id;
    draw();
  }));
  return box;
}

function drawEditor(c) {
  const box = make("div", "editor");
  let save;

  if (c.type === "item") {
    const product = make("select");
    product.append(new Option("(pick a product)", ""));
    for (const p of products) product.append(new Option(p.id + " - " + p.name, p.id));
    product.value = c.product_id || "";
    const quantity = make("input");
    quantity.type = "number";
    quantity.min = "1";
    quantity.step = "1";
    quantity.value = c.quantity;
    box.append(product, quantity);
    save = () => {
      const body = {quantity: Number(quantity.value)};
      if (!Number.isInteger(body.quantity) || body.quantity < 1) {
        say("The quantity must be a whole number, 1 or more.");
        return;
      }
      if (product.value) body.product_id = product.value;
      decide("/candidates/" + c.id + "/item", body);
    };
  } else {
    const input = make("input");
    input.type = "text";
    input.maxLength = 500;
    input.value = c.value || "";
    box.append(input);
    save = () => decide("/candidates/" + c.id + "/value", {value: input.value});
  }

  const buttons = make("div", "buttons");
  buttons.append(
    makeButton("Save", "main", save),
    makeButton("Cancel", "", () => {
      editingId = null;
      draw();
    }),
  );
  box.append(buttons);
  return box;
}

// --- Dictation ---------------------------------------------------------------------------

// Uses the browser's own speech recognition when it has one. The browser may
// send the audio to its maker to turn it into text. Without it, the Dictate
// button stays hidden and the note is typed (or dictated with the keyboard's
// microphone key).
function setUpDictation() {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) return;

  const recognition = new Recognition();
  recognition.lang = "en-US";
  recognition.continuous = true;
  let listening = false;

  recognition.addEventListener("result", (event) => {
    const box = byId("note-text");
    for (let i = event.resultIndex; i < event.results.length; i++) {
      if (event.results[i].isFinal) box.value += event.results[i][0].transcript.trim() + " ";
    }
  });
  recognition.addEventListener("error", (event) => say("Dictation stopped: " + event.error));
  recognition.addEventListener("end", () => {
    listening = false;
    byId("dictate").textContent = "Dictate";
  });

  byId("dictate").hidden = false;
  byId("dictate").addEventListener("click", () => {
    if (listening) {
      recognition.stop();
      return;
    }
    listening = true;
    byId("dictate").textContent = "Stop";
    say("");
    recognition.start();
  });
}

// --- Start ---------------------------------------------------------------------------------

async function start() {
  byId("submit").addEventListener("click", submitNote);
  byId("retry").addEventListener("click", tryAgain);
  byId("confirm-all").addEventListener("click", confirmAll);
  byId("new-note").addEventListener("click", newNote);
  setUpDictation();

  const answer = await call("GET", "/products");
  if (answer.ok) products = answer.data.products;

  window.addEventListener("hashchange", openNoteInAddress);
  await openNoteInAddress();
}

// Open the note named in the address (for example "#note=3"), if there is
// one. This is what makes a reload, or a typed address, reopen a note.
async function openNoteInAddress() {
  const match = /^#note=(\d{1,15})$/.exec(location.hash);
  if (match) await openNote(match[1]);
}

start();
