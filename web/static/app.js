"use strict";

// Answers and minutes are model-generated text. Everything below builds DOM
// nodes and assigns textContent - never innerHTML - so a document that happens
// to contain markup renders as the characters that were written.

const thread = document.getElementById("thread");
const form = document.getElementById("ask-form");
const questionInput = document.getElementById("question");
const modeSelect = document.getElementById("mode");
const localToggle = document.getElementById("local");
const submitButton = document.getElementById("submit");
const reader = document.getElementById("reader");
const readerTitle = document.getElementById("reader-title");
const readerBody = document.getElementById("reader-body");

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function readError(response) {
  try {
    const data = await response.json();
    if (typeof data.detail === "string") return data.detail;
    if (Array.isArray(data.detail) && data.detail.length) return data.detail[0].msg;
  } catch {
    // Non-JSON body; fall through to the status line.
  }
  return `Request failed (${response.status})`;
}

async function refreshHealth() {
  const node = document.getElementById("health");
  try {
    const response = await fetch("/api/health");
    const data = await response.json();
    const indexed = (data.manifest && data.manifest.indexed) || 0;
    if (!data.lightrag.reachable) {
      node.textContent = "LightRAG unreachable — run pipeline doctor";
      node.classList.add("bad");
      return;
    }
    node.classList.remove("bad");
    node.textContent = `${indexed} meeting${indexed === 1 ? "" : "s"} indexed`;
  } catch {
    node.textContent = "server unreachable";
    node.classList.add("bad");
  }
}

function citationChip(citation) {
  const label = citation.title
    ? `${citation.date || "undated"} · ${citation.title}`
    : citation.source;
  const chip = el("button", "cite", label);
  chip.type = "button";
  if (!citation.meeting_id) {
    // Retrieved, but its manifest row is gone. Still shown: the answer was
    // grounded in it, and silently dropping it would overstate the citations.
    chip.disabled = true;
    chip.title = "Retrieved, but no manifest row matches this file";
    return chip;
  }
  chip.title = citation.source;
  chip.addEventListener("click", () => openMinutes(citation.meeting_id));
  return chip;
}

function renderAnswer(turn, data) {
  turn.appendChild(el("div", "answer", data.answer));

  if (data.citations.length) {
    const row = el("div", "citations");
    row.appendChild(el("span", "label", "Sources"));
    data.citations.forEach((citation) => row.appendChild(citationChip(citation)));
    turn.appendChild(row);
  }

  const via = data.synthesized ? data.provider || "subscription chain" : "LightRAG (local)";
  const meta = el(
    "div",
    data.synthesized ? "meta" : "meta local",
    `${via} · retrieval ${data.retrieval_sec}s · synthesis ${data.synthesis_sec}s · ` +
      `${data.context_chars} chars of context`
  );
  turn.appendChild(meta);
}

async function openMinutes(meetingId) {
  readerTitle.textContent = "Loading…";
  readerBody.textContent = "";
  reader.showModal();
  try {
    const response = await fetch(`/api/meetings/${encodeURIComponent(meetingId)}/minutes`);
    if (!response.ok) {
      readerTitle.textContent = "Unavailable";
      readerBody.textContent = await readError(response);
      return;
    }
    const data = await response.json();
    readerTitle.textContent = `${data.date || "undated"} — ${data.title}`;
    readerBody.textContent = data.markdown;
    readerBody.scrollTop = 0;
  } catch (error) {
    readerTitle.textContent = "Unavailable";
    readerBody.textContent = String(error);
  }
}

async function ask(question) {
  const turn = el("div", "turn");
  turn.appendChild(el("div", "question", question));
  const pending = el("div", "answer", "Retrieving and synthesizing… this can take a while on CPU.");
  turn.appendChild(pending);
  thread.appendChild(turn);
  turn.scrollIntoView({ behavior: "smooth", block: "start" });

  submitButton.disabled = true;
  try {
    const response = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        mode: modeSelect.value || null,
        local: localToggle.checked,
      }),
    });
    pending.remove();
    if (!response.ok) {
      turn.appendChild(el("div", "answer error", await readError(response)));
      return;
    }
    renderAnswer(turn, await response.json());
  } catch (error) {
    pending.remove();
    turn.appendChild(el("div", "answer error", String(error)));
  } finally {
    submitButton.disabled = false;
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const question = questionInput.value.trim();
  if (!question) return;
  questionInput.value = "";
  ask(question);
});

questionInput.addEventListener("keydown", (event) => {
  // Enter sends; Shift+Enter is a newline. Questions are usually one line.
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

document.getElementById("reader-close").addEventListener("click", () => reader.close());

refreshHealth();
