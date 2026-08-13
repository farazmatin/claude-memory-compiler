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

// ── review queue ───────────────────────────────────────────────────────

const reviewList = document.getElementById("review-list");
const reviewCount = document.getElementById("review-count");
const showReviewed = document.getElementById("show-reviewed");
const reviewStatus = document.getElementById("review-status");

// Outcomes that remove a card from the list have to be reported outside it.
// Approving a meeting marks it reviewed, which drops it from the default queue -
// so a message rendered inside the card is destroyed by the refresh that follows,
// and the row appears to vanish for no stated reason.
function announce(text, isError) {
  reviewStatus.hidden = false;
  reviewStatus.textContent = text;
  reviewStatus.className = isError ? "banner error" : "banner ok";
}

function statusLine(item) {
  if (item.unresolved_labels.length) {
    const labels = item.unresolved_labels.join(", ");
    return `${item.unresolved_labels.length} unnamed speaker(s): ${labels} — action items from them have no owner`;
  }
  if (item.unresolved_in_minutes) return "A SPEAKER_ label survived into the minutes";
  if (item.reviewed) return "Reviewed";
  return "Names were inferred, not confirmed";
}

function speakerEditor(item, onSaved) {
  const grid = el("div", "speaker-grid");
  const inputs = new Map();
  item.speakers.forEach((speaker) => {
    grid.appendChild(el("label", "speaker-label", speaker.label));
    const input = el("input", "speaker-input");
    input.value = speaker.name || "";
    input.placeholder = "unknown";
    input.setAttribute("aria-label", `Name for ${speaker.label}`);
    if (!speaker.name) input.classList.add("missing");
    grid.appendChild(input);
    grid.appendChild(el("span", "confidence", speaker.confidence || ""));
    inputs.set(speaker.label, input);
  });

  const save = el("button", "action", "Save speakers");
  save.type = "button";
  save.addEventListener("click", async () => {
    const names = {};
    inputs.forEach((input, label) => (names[label] = input.value.trim()));
    save.disabled = true;
    try {
      const response = await fetch(`/api/meetings/${encodeURIComponent(item.meeting_id)}/speakers`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ names }),
      });
      if (!response.ok) {
        onSaved(await readError(response), true);
        return;
      }
      const body = await response.json();
      if (body.recompiling) {
        // The rewind drops this meeting out of the queue, so the card is about
        // to disappear - say why somewhere that survives the reload.
        announce(`${item.title}: ${body.detail}`, false);
        loadReview();
      } else {
        onSaved(body.detail, false);
      }
    } catch (error) {
      onSaved(String(error), true);
    } finally {
      save.disabled = false;
    }
  });

  const wrap = el("div", "editor");
  wrap.appendChild(grid);
  wrap.appendChild(save);
  return wrap;
}

function minutesEditor(item, onSaved) {
  const wrap = el("div", "editor");
  const area = el("textarea", "minutes-edit");
  area.rows = 18;
  area.value = "Loading…";
  area.disabled = true;
  wrap.appendChild(area);

  fetch(`/api/meetings/${encodeURIComponent(item.meeting_id)}/minutes`)
    .then(async (response) => {
      if (!response.ok) throw new Error(await readError(response));
      const data = await response.json();
      area.value = data.markdown;
      area.disabled = false;
    })
    .catch((error) => {
      area.value = String(error);
    });

  const row = el("div", "editor-actions");

  const save = el("button", "action", "Save minutes");
  save.type = "button";
  save.addEventListener("click", async () => {
    save.disabled = true;
    try {
      const response = await fetch(`/api/meetings/${encodeURIComponent(item.meeting_id)}/minutes`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ markdown: area.value }),
      });
      if (!response.ok) {
        onSaved(await readError(response), true);
        return;
      }
      onSaved("Saved. Approve to re-index, or leave it for the next pipeline run.", false);
    } catch (error) {
      onSaved(String(error), true);
    } finally {
      save.disabled = false;
    }
  });

  const approve = el("button", "action primary", "Approve & re-index");
  approve.type = "button";
  approve.addEventListener("click", async () => {
    approve.disabled = true;
    approve.textContent = "Indexing…";
    try {
      const response = await fetch(`/api/meetings/${encodeURIComponent(item.meeting_id)}/approve`, {
        method: "POST",
      });
      if (!response.ok) {
        onSaved(await readError(response), true);
        return;
      }
      announce(`Approved and re-indexed: ${item.title}`, false);
      loadReview();
    } catch (error) {
      onSaved(String(error), true);
    } finally {
      approve.disabled = false;
      approve.textContent = "Approve & re-index";
    }
  });

  row.appendChild(save);
  row.appendChild(approve);
  wrap.appendChild(row);
  return wrap;
}

function reviewCard(item) {
  const card = el("article", item.needs_attention ? "card attention" : "card");

  const head = el("div", "card-head");
  head.appendChild(el("h3", null, `${item.date || "undated"} — ${item.title}`));
  head.appendChild(el("span", "pill", item.status));
  card.appendChild(head);
  card.appendChild(el("p", item.needs_attention ? "status warn" : "status", statusLine(item)));

  const message = el("p", "message");
  message.hidden = true;
  const report = (text, isError) => {
    message.hidden = false;
    message.textContent = text;
    message.className = isError ? "message error" : "message ok";
  };

  card.appendChild(speakerEditor(item, report));
  card.appendChild(minutesEditor(item, report));
  card.appendChild(message);
  return card;
}

async function loadReview() {
  reviewList.textContent = "";
  reviewList.appendChild(el("p", "status", "Loading…"));
  try {
    const response = await fetch(`/api/review?include_reviewed=${showReviewed.checked}`);
    if (!response.ok) throw new Error(await readError(response));
    const { items } = await response.json();

    reviewList.textContent = "";
    const pending = items.filter((i) => i.needs_attention).length;
    reviewCount.hidden = pending === 0;
    reviewCount.textContent = String(pending);

    if (!items.length) {
      reviewList.appendChild(
        el("p", "status", "Nothing waiting. Meetings appear here once minutes are compiled.")
      );
      return;
    }
    items.forEach((item) => reviewList.appendChild(reviewCard(item)));
  } catch (error) {
    reviewList.textContent = "";
    reviewList.appendChild(el("p", "status warn", String(error)));
  }
}

showReviewed.addEventListener("change", loadReview);

// ── screens ────────────────────────────────────────────────────────────

const screens = {
  ask: document.getElementById("screen-ask"),
  review: document.getElementById("screen-review"),
};

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("is-active"));
    tab.classList.add("is-active");
    Object.entries(screens).forEach(([name, node]) => {
      node.hidden = name !== tab.dataset.screen;
    });
    if (tab.dataset.screen === "review") loadReview();
  });
});

refreshHealth();
loadReview();
