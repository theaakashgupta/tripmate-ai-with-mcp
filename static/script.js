let currentThreadId = localStorage.getItem("travel_thread_id") || null;
let latestAnswerMarkdown = "";
let currentApproval = null;

function setPrompt(text) {
    document.getElementById("userInput").value = text;
}

function setLoading(isLoading) {
    const sendBtn = document.getElementById("sendBtn");
    const btnText = document.getElementById("btnText");
    const btnLoader = document.getElementById("btnLoader");

    sendBtn.disabled = isLoading;

    if (isLoading) {
        btnText.classList.add("hidden");
        btnLoader.classList.remove("hidden");
    } else {
        btnText.classList.remove("hidden");
        btnLoader.classList.add("hidden");
    }
}

function showError(message) {
    const errorBox = document.getElementById("errorBox");

    errorBox.textContent = message;
    errorBox.classList.remove("hidden");
}

function hideError() {
    const errorBox = document.getElementById("errorBox");

    errorBox.classList.add("hidden");
    errorBox.textContent = "";
}

function showResult(answer, threadId, booking) {
    latestAnswerMarkdown = answer;

    const resultSection = document.getElementById("resultSection");
    const resultBox = document.getElementById("resultBox");
    const threadInfo = document.getElementById("threadInfo");

    if (typeof marked !== "undefined") {
        resultBox.innerHTML = marked.parse(answer);
    } else {
        resultBox.innerText = answer;
    }

    threadInfo.textContent = `Thread ID: ${threadId}`;

    renderBooking(booking);

    resultSection.classList.remove("hidden");

    resultSection.scrollIntoView({
        behavior: "smooth",
        block: "start"
    });
}

function fillBookingLinks(containerId, links) {
    const container = document.getElementById(containerId);

    container.innerHTML = "";

    (links || []).forEach(function(link) {
        const anchor = document.createElement("a");

        anchor.href = link.url;
        anchor.textContent = `Book with ${link.name}`;
        anchor.target = "_blank";
        anchor.rel = "noopener noreferrer";

        container.appendChild(anchor);
    });
}

function renderBooking(booking) {
    const section = document.getElementById("bookingSection");

    if (!booking || !(booking.flights || booking.hotels)) {
        section.classList.add("hidden");
        return;
    }

    const route = booking.origin && booking.destination
        ? `${booking.origin} → ${booking.destination}`
        : (booking.destination || "");

    document.getElementById("flightRoute").textContent = route;
    document.getElementById("hotelCity").textContent = booking.destination || "";

    fillBookingLinks("flightLinks", booking.flights);
    fillBookingLinks("hotelLinks", booking.hotels);

    section.classList.remove("hidden");
}

async function sendMessage() {
    hideError();

    const input = document.getElementById("userInput");
    const message = input.value.trim();

    if (!message) {
        showError("Please enter your travel request first.");
        return;
    }

    setLoading(true);

    try {
        const response = await fetch("/api/travel", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                message: message,
                thread_id: currentThreadId
            })
        });

        const data = await response.json();

        if (!response.ok || !data.success) {
            throw new Error(data.error || "Something went wrong.");
        }

        currentThreadId = data.thread_id;
        localStorage.setItem("travel_thread_id", currentThreadId);

        showResult(data.answer, data.thread_id, data.booking_links);
        renderBookingState(data);
    } catch (error) {
        showError(error.message);
    } finally {
        setLoading(false);
    }
}

function copyResult() {
    const resultBox = document.getElementById("resultBox");
    const text = resultBox.innerText;

    if (!text) {
        return;
    }

    navigator.clipboard.writeText(text)
        .then(() => {
            const copyBtn = document.querySelector(".copy-btn");
            const oldText = copyBtn.textContent;

            copyBtn.textContent = "Copied!";

            setTimeout(() => {
                copyBtn.textContent = oldText;
            }, 1400);
        })
        .catch(() => {
            showError("Could not copy result.");
        });
}

function downloadPDF() {
    const pdfContent = document.getElementById("pdfContent");

    if (!latestAnswerMarkdown || !pdfContent) {
        showError("No travel plan available to download.");
        return;
    }

    const downloadBtn = document.querySelector(".download-btn");
    const oldText = downloadBtn.textContent;

    downloadBtn.textContent = "Preparing PDF...";
    downloadBtn.disabled = true;

    const options = {
        margin: 0.5,
        filename: "ai-travel-plan.pdf",
        image: {
            type: "jpeg",
            quality: 0.98
        },
        html2canvas: {
            scale: 2,
            useCORS: true,
            backgroundColor: "#ffffff"
        },
        jsPDF: {
            unit: "in",
            format: "a4",
            orientation: "portrait"
        },
        pagebreak: {
            mode: ["avoid-all", "css", "legacy"]
        }
    };

    html2pdf()
        .set(options)
        .from(pdfContent)
        .save()
        .then(() => {
            downloadBtn.textContent = oldText;
            downloadBtn.disabled = false;
        })
        .catch(() => {
            downloadBtn.textContent = oldText;
            downloadBtn.disabled = false;
            showError("Could not download PDF.");
        });
}

document.addEventListener("keydown", function(event) {
    if (event.ctrlKey && event.key === "Enter") {
        sendMessage();
    }
});


// =========================
// Booking workflow UI
// =========================

function renderBookingState(data) {
    renderStatusBadge(data);
    renderProgress(data.progress, data.tasks);

    if (data.needs_decision && data.approval) {
        currentApproval = data.approval;
        renderApproval(data.approval);
    } else {
        currentApproval = null;
        document.getElementById("approvalSection").classList.add("hidden");
    }

    renderRecords(data.booking_records, data.receipt);
}


function renderStatusBadge(data) {
    const bar = document.getElementById("statusBar");
    const badge = document.getElementById("statusBadge");
    const hint = document.getElementById("statusHint");

    const status = data.booking_status || "DRAFT";
    const label = data.status_label || status;

    badge.textContent = label;
    badge.className = "status-badge status-" + String(status).toLowerCase();

    let text = "";
    if (data.needs_decision) {
        text = "Waiting for your explicit approval — nothing has been purchased.";
    } else if (status === "BOOKED") {
        text = "Every approved purchase was confirmed by the provider.";
    } else if (status === "BOOKING_FAILED") {
        text = "The provider did not confirm everything — unconfirmed items were not charged.";
    } else if (status === "CANCELLED") {
        text = "Cancellation confirmed by the provider.";
    } else if (status === "OPTIONS_READY") {
        text = "Options ready. Say \"book it\" to prepare them for your approval.";
    }
    hint.textContent = text;

    bar.classList.remove("hidden");
}


function renderProgress(progress, tasks) {
    const box = document.getElementById("progressBox");
    const list = document.getElementById("progressList");
    const lines = [];

    (progress || []).forEach(function(line) {
        lines.push({ text: line, status: "done" });
    });

    if (!lines.length) {
        (tasks || []).forEach(function(task) {
            if (task.status === "pending") {
                lines.push({ text: "Pending: " + task.label, status: "pending" });
            }
        });
    }

    if (!lines.length) {
        box.classList.add("hidden");
        return;
    }

    list.innerHTML = "";
    lines.slice(-8).forEach(function(item) {
        const li = document.createElement("li");
        li.className = "progress-item progress-" + item.status;
        li.textContent = item.text;
        list.appendChild(li);
    });

    box.classList.remove("hidden");
}


function money(amount, currency) {
    return Number(amount || 0).toFixed(2) + " " + (currency || "");
}


function escapeHtml(value) {
    return String(value == null ? "" : value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}


function purchaseCard(purchase) {
    const option = purchase.option || {};
    const quote = purchase.quote || {};
    const conditions = quote.conditions || {};
    const approval = purchase.approval || {};

    const card = document.createElement("div");
    card.className = "approval-card";

    const kindLabel = purchase.kind === "flight" ? "✈️ Flight" : "🏨 Hotel";
    const rows = (quote.components || []).map(function(component) {
        return "<tr><td>" + escapeHtml(component.label) + "</td>" +
               '<td class="amount">' + money(component.amount, quote.currency) +
               "</td></tr>";
    }).join("");

    const facts = [];
    if (quote.dates) {
        Object.keys(quote.dates).forEach(function(key) {
            if (quote.dates[key]) {
                facts.push(key.replace(/_/g, " ") + ": " + quote.dates[key]);
            }
        });
    }
    if (quote.room_type) facts.push("Room type: " + quote.room_type);
    if (conditions.fare_brand) facts.push("Fare brand: " + conditions.fare_brand);
    if (conditions.baggage) facts.push("Baggage: " + conditions.baggage);
    if (conditions.board) facts.push("Board: " + conditions.board);
    if (conditions.cancellation_policy) {
        facts.push("Cancellation: " + conditions.cancellation_policy);
    }
    if (conditions.changes) facts.push("Changes: " + conditions.changes);

    card.innerHTML =
        '<div class="approval-card-head">' +
            '<span class="approval-kind">' + kindLabel + "</span>" +
            '<span class="approval-price">' + money(quote.total, quote.currency) + "</span>" +
        "</div>" +
        "<h4>" + escapeHtml(option.title || "Selected option") + "</h4>" +
        '<p class="approval-sub">' + escapeHtml(option.subtitle || "") + "</p>" +
        '<ul class="approval-facts">' +
            facts.map(function(fact) {
                return "<li>" + escapeHtml(fact) + "</li>";
            }).join("") +
        "</ul>" +
        '<table class="approval-table">' +
            "<thead><tr><th>Price breakdown</th><th>Amount</th></tr></thead>" +
            "<tbody>" + rows + "</tbody>" +
            "<tfoot><tr><td><strong>Total</strong></td>" +
            '<td class="amount"><strong>' + money(quote.total, quote.currency) +
            "</strong></td></tr></tfoot>" +
        "</table>" +
        '<p class="approval-meta">Approval <code>' +
            escapeHtml(approval.approval_id || "") + "</code>" +
            (quote.availability === false ? " · <strong>unavailable</strong>" : "") +
        "</p>" +
        '<div class="approval-card-actions">' +
            '<button class="approve-btn" onclick="approveOne(\'' +
                escapeHtml(approval.approval_id) + '\')">Approve this purchase</button>' +
            '<button class="reject-btn" onclick="rejectOne(\'' +
                escapeHtml(approval.approval_id) + '\')">Reject</button>' +
        "</div>";

    return card;
}


function renderApproval(approval) {
    const section = document.getElementById("approvalSection");
    const notice = document.getElementById("approvalNotice");
    const cards = document.getElementById("approvalCards");
    const totalBox = document.getElementById("approvalTotal");

    cards.innerHTML = "";
    totalBox.innerHTML = "";

    if (approval.type === "cancellation_approval") {
        const booking = approval.booking || {};
        const estimate = approval.estimate || {};

        notice.innerHTML =
            "<strong>Cancel this booking?</strong><br>Booking <code>" +
            escapeHtml(booking.reference || "") + "</code> — " +
            escapeHtml(booking.title || booking.kind || "") + "<br>Paid: " +
            money(estimate.amount_paid, estimate.currency) +
            " · Cancellation fee: " + money(estimate.cancellation_fee, estimate.currency) +
            " · Refund due: " + money(estimate.refund, estimate.currency);
        notice.classList.remove("hidden");

        document.getElementById("approveAllBtn").textContent = "Approve cancellation";
        document.getElementById("rejectAllBtn").textContent = "Keep my booking";
        document.getElementById("modifyBtn").classList.add("hidden");
        document.getElementById("modifyInput").classList.add("hidden");
    } else {
        if (approval.reapproval_required) {
            notice.innerHTML = "<strong>⚠️ Approval required again</strong><br>" +
                (approval.reasons || []).map(escapeHtml).join("<br>");
            notice.classList.remove("hidden");
        } else {
            notice.classList.add("hidden");
        }

        (approval.purchases || []).forEach(function(purchase) {
            cards.appendChild(purchaseCard(purchase));
        });

        const total = approval.total || {};
        totalBox.innerHTML = "Estimated trip total: <strong>" +
            money(total.total, total.currency) + "</strong>";

        document.getElementById("approveAllBtn").textContent = "Approve all";
        document.getElementById("rejectAllBtn").textContent = "Reject all";
        document.getElementById("modifyBtn").classList.remove("hidden");
    }

    section.classList.remove("hidden");
    section.scrollIntoView({ behavior: "smooth", block: "start" });
}


function renderRecords(records, receipt) {
    const section = document.getElementById("recordsSection");
    const list = document.getElementById("recordsList");
    const receiptBox = document.getElementById("receiptBox");
    const cancelBtn = document.getElementById("cancelBookingBtn");

    const active = (records || []).filter(function(record) {
        return record.reference;
    });

    if (!active.length && !receipt) {
        section.classList.add("hidden");
        return;
    }

    list.innerHTML = "";
    active.forEach(function(record) {
        const row = document.createElement("div");
        row.className = "record-row status-" + String(record.status || "").toLowerCase();
        row.innerHTML =
            '<span class="record-kind">' + escapeHtml(record.kind || "booking") + "</span>" +
            "<code>" + escapeHtml(record.reference) + "</code>" +
            '<span class="record-title">' + escapeHtml(record.title || "") + "</span>" +
            '<span class="record-status">' + escapeHtml(record.status || "") + "</span>";
        list.appendChild(row);
    });

    if (receipt) {
        receiptBox.innerHTML = (typeof marked !== "undefined")
            ? marked.parse(receipt)
            : escapeHtml(receipt);
        receiptBox.classList.remove("hidden");
    } else {
        receiptBox.classList.add("hidden");
    }

    const hasConfirmed = active.some(function(record) {
        return record.status === "confirmed";
    });
    cancelBtn.classList.toggle("hidden", !hasConfirmed);

    section.classList.remove("hidden");
}


async function postBooking(url, body) {
    const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok || !data.success) {
        throw new Error(data.error || "Request failed.");
    }
    return data;
}


async function submitApproval(payload) {
    hideError();
    setLoading(true);
    try {
        const data = await postBooking("/api/booking/approve", {
            thread_id: currentThreadId,
            action: payload.action,
            approval_id: payload.approval_id || null,
            modifications: payload.modifications || null
        });
        showResult(data.answer, data.thread_id, data.booking_links || {});
        renderBookingState(data);
    } catch (error) {
        showError(error.message);
    } finally {
        setLoading(false);
    }
}


function isCancellationApproval() {
    return !!(currentApproval && currentApproval.type === "cancellation_approval");
}


function cancelApprovalId() {
    return (currentApproval.approval || {}).approval_id;
}


function approveAll() {
    if (isCancellationApproval()) {
        submitApproval({ action: "approve", approval_id: cancelApprovalId() });
        return;
    }
    submitApproval({ action: "approve" });
}


function rejectAll() {
    if (isCancellationApproval()) {
        submitApproval({ action: "reject", approval_id: cancelApprovalId() });
        return;
    }
    submitApproval({ action: "reject" });
}


function approveOne(approvalId) {
    submitApproval({ action: "approve", approval_id: approvalId });
}


function rejectOne(approvalId) {
    submitApproval({ action: "reject", approval_id: approvalId });
}


function sendModification() {
    const input = document.getElementById("modifyInput");

    if (input.classList.contains("hidden")) {
        input.classList.remove("hidden");
        input.focus();
        return;
    }

    const text = input.value.trim();
    if (!text) {
        showError("Describe what you want changed first.");
        return;
    }

    submitApproval({ action: "modify", modifications: text }).then(function() {
        input.value = "";
        input.classList.add("hidden");
    });
}


async function requestCancel() {
    if (!currentThreadId) return;
    hideError();
    setLoading(true);
    try {
        const data = await postBooking("/api/booking/cancel", {
            thread_id: currentThreadId
        });
        showResult(data.answer, data.thread_id, data.booking_links || {});
        renderBookingState(data);
    } catch (error) {
        showError(error.message);
    } finally {
        setLoading(false);
    }
}