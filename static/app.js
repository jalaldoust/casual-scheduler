const GPU_COUNT = 8;

const state = {
  session: null,
  overview: null,
  selectedWeek: null,
  selectedDay: null,
  userSelectedDay: false,
  userSelectedWeekType: false,
  selectedWeekType: null,
  currentWeekKey: null,
  nextWeekKey: null,
  weekCache: new Map(),
  mySummary: null,
  myBids: [],
  serverNow: null,
  today: null,
  outbid: {
    visible: false,
    items: [],
  },
  prefetchedWeeks: new Set(),
  admin: {
    users: [],
    weeks: [],
  },
  countdownInterval: null,
  autoRefreshInterval: null,
  bulkSelect: {
    isSelecting: false,
    startCell: null,
    endCell: null,
    cells: new Set(),
  },
  lastBid: null, // Track last bid for undo feature
};

function updateServerClock(meta) {
  const iso = meta?.now || null;
  if (iso) {
    state.serverNow = iso;
    state.today = iso.split("T")[0];
  } else {
    const localIso = new Date().toISOString();
    state.serverNow = localIso;
    state.today = localIso.split("T")[0];
  }
}

// Fetch and render live GPU status
async function fetchAndRenderGPUStatus() {
  try {
    const data = await fetchJson('/api/gpu-live-status');
    if (data.ok) {
      renderGPUStatusTiles(data.usage, data.gpu_count);
    }
  } catch (err) {
    console.error('Failed to fetch GPU status:', err);
  }
}

function renderGPUStatusTiles(usage, gpuCount = GPU_COUNT) {
  // Update header tiles if present
  const headerContainer = document.getElementById('header-gpu-status');
  if (headerContainer) {
    // For header, add numbers and usernames
    const headerTiles = [];
    for (let i = 0; i < gpuCount; i++) {
      const users = usage[String(i)] || [];
      const displayName = users.length > 0 ? users.join(', ') : '--';
      headerTiles.push(`
        <div class="gpu-header-tile">
          <div class="gpu-number">${i}</div>
          <div class="gpu-name">${displayName}</div>
        </div>
      `);
    }
    headerContainer.innerHTML = headerTiles.join('');
  }
}

function getServerTimeContext() {
  if (state.serverNow) {
    const [datePart, timePartRaw = ""] = state.serverNow.split("T");
    const today = state.today || datePart;
    const hourMatch = timePartRaw.match(/(\d{2}):/);
    const hour = hourMatch ? parseInt(hourMatch[1], 10) : null;
    return { today, hour };
  }
  const now = new Date();
  return { today: now.toISOString().split("T")[0], hour: now.getHours() };
}

document.addEventListener("DOMContentLoaded", () => {
  bootstrap();
});

async function bootstrap() {
  await loadSession();
}

async function loadSession() {
  const resp = await fetchJson("/api/session");
  if (resp.authenticated) {
    state.session = resp.user;
    await refreshData();
  } else {
    state.session = null;
    render();
  }
}

async function updateTransitionHourDisplay() {
  if (state.session?.role !== "admin") return;

  try {
    const resp = await fetchJson("/api/admin/transition-hour");
    const displayEl = document.getElementById("transitionHourDisplay");

    if (displayEl) {
      const hour = resp.transition_hour;
      displayEl.textContent = `Days start at: ${String(hour).padStart(2, '0')}:00`;
    }
  } catch (err) {
    console.error("Failed to update transition hour display:", err);
  }
}

async function refreshData() {
  try {
    state.overview = await fetchJson("/api/overview");
  } catch (err) {
    console.error(err);
    alert(err.message);
    return;
  }
  state.session = state.overview.user;
  updateServerClock(state.overview);

  const activeWeekKey = syncWeekSelection();
  if (activeWeekKey) {
    await loadWeekDay(activeWeekKey, state.selectedDay);
  }
  await loadPersonalData();
  if (state.session.role === "admin") {
    await loadAdminData();
  }
  startCountdown();
  startAutoRefresh();
  render();
}

function startAutoRefresh() {
  // Clear existing interval
  if (state.autoRefreshInterval) {
    clearInterval(state.autoRefreshInterval);
    state.autoRefreshInterval = null;
  }

  // Only auto-refresh when viewing current (executing) week
  const currentWeekKey = state.currentWeekKey;
  if (state.selectedWeek === currentWeekKey && state.selectedWeekType === "current") {
    // Refresh every 30 seconds to show live GPU usage updates
    state.autoRefreshInterval = setInterval(async () => {
      try {
        // Silently refresh the current day data
        if (state.selectedWeek && state.selectedDay) {
          const query = new URLSearchParams({ week: state.selectedWeek, day: state.selectedDay });
          const data = await fetchJson(`/api/week?${query.toString()}`);
          const cacheKey = `${state.selectedWeek}|${state.selectedDay}`;
          state.weekCache.set(cacheKey, data);
          render();
        }
        // Also refresh GPU status in header
        fetchAndRenderGPUStatus();
      } catch (err) {
        console.error("Auto-refresh failed:", err);
      }
    }, 30000); // 30 seconds
  }
}

function stopAutoRefresh() {
  if (state.autoRefreshInterval) {
    clearInterval(state.autoRefreshInterval);
    state.autoRefreshInterval = null;
  }
}

async function loadWeekDay(weekKey, day) {
  if (!weekKey) {
    return;
  }
  const effectiveDay = day || getWeekDays(weekKey)[0];
  const query = new URLSearchParams({ week: weekKey });
  if (effectiveDay) {
    query.set("day", effectiveDay);
  }
  const cacheKey = `${weekKey}|${effectiveDay}`;
  const data = await fetchJson(`/api/week?${query.toString()}`);
  state.weekCache.set(cacheKey, data);
  if (!day) {
    state.selectedDay = effectiveDay;
    state.userSelectedDay = false;
  }
  if (weekKey === state.nextWeekKey) {
    state.selectedWeekType = "next";
  } else if (weekKey === state.currentWeekKey) {
    state.selectedWeekType = "current";
  }
  if (!state.prefetchedWeeks.has(weekKey)) {
    await prefetchWeek(weekKey);
    state.prefetchedWeeks.add(weekKey);
  }
  updateOutbidSummary(weekKey);
}

async function loadAdminData() {
  try {
    const [usersResp, weeksResp] = await Promise.all([
      fetchJson("/api/admin/users"),
      fetchJson("/api/admin/weeks"),
    ]);
    state.admin.users = usersResp.users || [];
    state.admin.weeks = weeksResp.weeks || [];
  } catch (err) {
    console.error(err);
    alert(err.message);
  }
}

async function prefetchWeek(weekKey) {
  const days = getWeekDays(weekKey);
  const tasks = [];
  for (const day of days) {
    const cacheKey = `${weekKey}|${day}`;
    if (state.weekCache.has(cacheKey)) {
      continue;
    }
    const query = new URLSearchParams({ week: weekKey, day });
    tasks.push(
      fetchJson(`/api/week?${query.toString()}`)
        .then((data) => {
          state.weekCache.set(cacheKey, data);
          if (weekKey === state.nextWeekKey || weekKey === state.currentWeekKey) {
            updateOutbidSummary(weekKey);
          }
        })
        .catch((err) => {
          console.error("Failed to prefetch day", day, err);
        })
    );
  }
  if (tasks.length) {
    await Promise.all(tasks);
  }
}

function startCountdown() {
  if (state.countdownInterval) {
    clearInterval(state.countdownInterval);
  }
  // Show countdown to midnight (end of current day / day advancement)
  const currentDay = (state.overview.weeks || []).find((w) => w.status === "executing");
  const targetTime = currentDay ? new Date(currentDay.close_at).getTime() : null;
  if (!targetTime) {
    updateCountdownLabel("—");
    return;
  }
  const tick = () => {
    const now = Date.now();
    const diff = targetTime - now;
    if (diff <= 0) {
      updateCountdownLabel("Day advancing...");
      clearInterval(state.countdownInterval);
      state.countdownInterval = null;
      // Reload to get new day
      setTimeout(() => window.location.reload(), 2000);
      return;
    }
    const hours = Math.floor(diff / (3600 * 1000));
    const minutes = Math.floor((diff % (3600 * 1000)) / (60 * 1000));
    const seconds = Math.floor((diff % (60 * 1000)) / 1000);
    const label = `${pad(hours)}:${pad(minutes)}:${pad(seconds)}`;
    updateCountdownLabel(label);
  };
  tick();
  state.countdownInterval = setInterval(tick, 1000);
}

function updateCountdownLabel(text) {
  const el = document.querySelector("[data-countdown]");
  if (el) {
    el.textContent = text;
  }
}

function pad(value) {
  return String(value).padStart(2, "0");
}

function hourLabelText(hour) {
  return `${pad(hour)}:00 – ${pad((hour + 1) % 24)}:00`;
}

function formatDayLabel(dayStr) {
  // Parse date in UTC to avoid timezone shifts
  return new Date(`${dayStr}T12:00:00Z`).toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    timeZone: "America/New_York"
  });
}

function formatOutbidLabel(slotKey, gpu, price, winner) {
  const [dayStr, timeStr] = slotKey.split("T");
  const hour = parseInt(timeStr.slice(0, 2), 10);
  const dayLabel = formatDayLabel(dayStr);
  return `${dayLabel} • ${hourLabelText(hour)} • GPU ${gpu} • ${price} cr by ${winner}`;
}

function updateOutbidSummary(weekKey) {
  if (!state.session || !weekKey) {
    state.outbid.visible = false;
    state.outbid.items = [];
    return;
  }

  const weekMeta = (state.overview?.weeks || []).find((w) => w.week_start === weekKey);

  // Only show notifications if backend says there are notifications
  if (!weekMeta || !weekMeta.has_notifications) {
    state.outbid.visible = false;
    state.outbid.items = [];
    return;
  }

  // ONLY show outbid notifications for currently open (planning) days
  if (weekMeta.status !== "open") {
    state.outbid.visible = false;
    state.outbid.items = [];
    return;
  }

  const username = state.session.username;
  const items = [];

  // Find all outbid slots from cache for this day
  for (const [cacheKey, data] of state.weekCache.entries()) {
    if (!cacheKey.startsWith(`${weekKey}|`)) {
      continue;
    }

    // Get notification queue from the week data
    const notificationQueue = data.outbid_notification_queue || [];

    data.rows.forEach((row) => {
      row.entries.forEach((entry) => {
        const slotKey = `${weekKey}|${row.slot}|${entry.gpu}`;

        // Only show notifications for slots in the queue
        if (notificationQueue.includes(slotKey)) {
          items.push({
            key: slotKey,
            label: formatOutbidLabel(row.slot, entry.gpu, entry.price, entry.winner),
          });
        }
      });
    });
  }

  state.outbid.items = items;
  state.outbid.visible = items.length > 0;
}

async function fetchJson(url, options = {}) {
  const resp = await fetch(url, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (resp.headers.get("Content-Type")?.includes("application/json")) {
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.error || "Request failed");
    }
    return data;
  }
  if (!resp.ok) {
    throw new Error(`Request failed: ${resp.status}`);
  }
  return {};
}

function render() {
  const app = document.getElementById("app");
  if (!state.session) {
    app.innerHTML = renderLogin();
    const form = app.querySelector("form");
    form.addEventListener("submit", onLoginSubmit);
    return;
  }
  const weekData = getCurrentWeekData();
  app.innerHTML = `
    <div class="layout">
      ${renderHeader()}
      <div class="main">
        <div class="panel">
          ${renderOutbidBanner()}
          ${renderUndoBanner()}
          ${renderWeekSelector()}
          ${weekData ? renderDayTabs(weekData) : ""}
          ${weekData ? renderGrid(weekData) : renderEmptyGrid()}
        </div>
        <aside class="sidebar">
          ${renderBalances()}
          ${renderPasswordChange()}
          ${state.session.role === "admin" ? renderAdminPanel() : ""}
        </aside>
      </div>
    </div>
  `;
  bindInteractions();
  // Fetch and display GPU status in header
  fetchAndRenderGPUStatus();
}

function renderLogin() {
  return `
    <section class="login">
      <h1>CausalAI HPC Schedule</h1>
      <p class="subtitle">A non-casual approach to resource allocation</p>
      <form>
        <label>
          Username
          <input type="text" name="username" required>
        </label>
        <label>
          Password
          <input type="password" name="password" required>
        </label>
        <button type="submit">Sign in</button>
      </form>
    </section>
  `;
}

function renderOutbidBanner() {
  if (!state.outbid.visible || !state.outbid.items.length) {
    return "";
  }
  const listItems = state.outbid.items
    .map((item) => `<li data-outbid-key="${item.key}">${item.label}</li>`)
    .join("");
  return `
    <div class="notice outbid-alert">
      <div class="notice-body">
        <strong>You were outbid on:</strong>
        <ul>${listItems}</ul>
      </div>
      <button type="button" data-action="ack-outbid">Got it</button>
    </div>
  `;
}

function renderUndoBanner() {
  if (!state.lastBid) {
    return "";
  }

  // Only show undo for 10 seconds after bid
  const elapsed = Date.now() - state.lastBid.timestamp;
  if (elapsed > 10000) {
    state.lastBid = null;
    return "";
  }

  const slotLabel = state.lastBid.slot.replace("T", " ");
  const wasEmpty = !state.lastBid.previousWinner;
  const message = wasEmpty
    ? `Bid placed on ${slotLabel} • GPU ${state.lastBid.gpu}`
    : `Bid raised on ${slotLabel} • GPU ${state.lastBid.gpu}`;

  return `
    <div class="notice undo-alert">
      <div class="notice-body">
        <strong>${message}</strong>
      </div>
      <button type="button" data-action="undo-bid">Undo</button>
    </div>
  `;
}

function renderHeader() {
  return `
    <header class="header">
      <div>
        <h1>CausalAI HPC Schedule</h1>
        <p class="subtitle">A non-casual approach to resource allocation</p>
        <div id="header-gpu-status" class="header-gpu-tiles"></div>
      </div>
      <div>
        <p>Signed in as <strong>${state.session.username}</strong> (${state.session.role})</p>
        <p>Next day in: <span data-countdown>—</span></p>
        <button id="historyBtn" style="margin-right: 8px;">View History</button>
        <button id="logoutBtn">Sign out</button>
      </div>
    </header>
  `;
}

function renderWeekSelector() {
  const days = state.overview.weeks || [];
  const currentDay = days.find(d => d.status === "executing");
  const openDays = days.filter(d => d.status === "open");

  const allDays = [];
  if (currentDay) allDays.push(currentDay);
  allDays.push(...openDays);

  const buttons = allDays.map((day, index) => {
    const isCurrent = day.status === "executing";
    // Use compact date format with weekday (e.g., "Mon, Nov 24")
    const dateObj = new Date(`${day.day}T12:00:00Z`);
    const weekday = dateObj.toLocaleDateString(undefined, {
      weekday: "short",
      timeZone: "America/New_York"
    });
    const date = dateObj.toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
      timeZone: "America/New_York"
    });
    const label = `${weekday}, ${date}`;
    const type = isCurrent ? "current" : `open-${index}`;
    return renderDayButton(type, label, day);
  }).join("");

  return `
    <section class="week-selector">
      <h2>Select Day</h2>
      <div class="button-row" style="flex-wrap: wrap; gap: 8px;">${buttons}</div>
    </section>
  `;
}

function hasNotificationsForDay(weekKey) {
  // Check overview data for has_notifications flag
  const weekMeta = (state.overview?.weeks || []).find((w) => w.week_start === weekKey);
  return weekMeta?.has_notifications || false;
}

function renderDayButton(type, label, meta) {
  if (!meta) {
    return `<button class="disabled" disabled>${label}<span class="week-detail">Unavailable</span></button>`;
  }
  const selected = state.selectedWeek === meta.week_start ? "selected" : "";
  const isCurrent = type === "current" ? "current-day" : "";
  const hasNotif = hasNotificationsForDay(meta.week_start) ? "has-notifications" : "";
  const statusLabel = formatStatus(meta.status);
  return `<button class="${selected} ${isCurrent} ${hasNotif}" data-week-type="${type}" data-week="${meta.week_start}">
    ${label}
    <span class="week-detail">${statusLabel}</span>
  </button>`;
}

function findWeekMeta(weekKey) {
  return (state.overview.weeks || []).find((w) => w.week_start === weekKey);
}

function formatStatus(status) {
  switch (status) {
    case "open":
      return "Planning";
    case "executing":
      return "In progress";
    case "final":
      return "Finalized";
    case "future":
      return "Upcoming";
    default:
      return status;
  }
}

function formatBidStatus(status) {
  switch (status) {
    case "leading":
      return "Leading";
    case "lost":
      return "Outbid";
    case "open":
      return "Open";
    default:
      return status || "—";
  }
}

function syncWeekSelection() {
  const weeks = state.overview?.weeks || [];
  state.currentWeekKey = null;
  state.nextWeekKey = null;
  let openWeek = null;
  for (const wk of weeks) {
    if (!state.currentWeekKey && wk.status === "executing") {
      state.currentWeekKey = wk.week_start;
    }
    if (!openWeek && wk.status === "open") {
      openWeek = wk;
    }
    if (!state.nextWeekKey && (wk.status === "open" || wk.status === "future")) {
      state.nextWeekKey = wk.week_start;
    }
  }
  if (!state.currentWeekKey) {
    const fallback = weeks.find((wk) => wk.status !== "future");
    if (fallback) {
      state.currentWeekKey = fallback.week_start;
    }
  }
  if (!state.nextWeekKey) {
    const future = weeks.find((wk) => wk.status === "future");
    if (future) {
      state.nextWeekKey = future.week_start;
    }
  }
  const { today: todayStrContext } = getServerTimeContext();
  const currentWeekHasToday =
    todayStrContext && state.currentWeekKey
      ? getWeekDays(state.currentWeekKey).includes(todayStrContext)
      : false;
  const nextWeekHasToday =
    todayStrContext && state.nextWeekKey
      ? getWeekDays(state.nextWeekKey).includes(todayStrContext)
      : false;
  if (!state.userSelectedWeekType || !state.selectedWeekType) {
    if (currentWeekHasToday) {
      state.selectedWeekType = "current";
    } else if (nextWeekHasToday) {
      state.selectedWeekType = "next";
    } else if (state.currentWeekKey) {
      state.selectedWeekType = "current";
    } else if (state.nextWeekKey) {
      state.selectedWeekType = "next";
    }
  }
  if (state.selectedWeekType === "current" && !state.currentWeekKey && state.nextWeekKey) {
    state.selectedWeekType = "next";
    state.userSelectedWeekType = false;
  }
  if (state.selectedWeekType === "next" && !state.nextWeekKey && state.currentWeekKey) {
    state.selectedWeekType = "current";
    state.userSelectedWeekType = false;
  }
  const activeWeekKey =
    state.selectedWeekType === "current" ? state.currentWeekKey : state.nextWeekKey;
  state.selectedWeek = activeWeekKey || null;
  if (!activeWeekKey) {
    state.selectedDay = null;
    state.userSelectedDay = false;
    return null;
  }
  const days = getWeekDays(activeWeekKey);
  const todayStr = state.today;
  let autoSelected = false;
  if (!state.userSelectedDay && todayStr && days.includes(todayStr)) {
    state.selectedDay = todayStr;
    autoSelected = true;
  } else if (!state.selectedDay || !days.includes(state.selectedDay)) {
    state.selectedDay = days[0];
    autoSelected = true;
  }
  if (autoSelected) {
    state.userSelectedDay = false;
  }
  return activeWeekKey;
}

function renderDayTabs(weekData) {
  // No more day tabs - each day view shows only one day
  // Just show the current day being viewed
  const dayLabel = new Date(`${weekData.day}T12:00:00Z`).toLocaleDateString(undefined, {
    weekday: "long",
    month: "long",
    day: "numeric",
    year: "numeric",
    timeZone: "America/New_York"
  });
  const statusLabel = formatStatus(weekData.status);
  return `
    <section class="day-header">
      <h3>${dayLabel} <span class="status-badge">${statusLabel}</span></h3>
    </section>
  `;
}

function renderGrid(weekData) {
  const status = weekData.status;
  const allowBidding = status === "open";
  const allowReleasing = status === "executing"; // Can release from current executing week
  const rowByHour = new Map(weekData.rows.map((row) => [row.hour, row]));

  // Check if this day has active (non-dismissed) notifications
  const weekMeta = (state.overview?.weeks || []).find((w) => w.week_start === weekData.week_start);
  const hasActiveNotifications = weekMeta?.has_notifications || false;

  // Reorder hours to start from transition hour
  const transitionHour = state.overview?.transition_hour || 0;
  const hours = Array.from({ length: 24 }, (_, i) => (transitionHour + i) % 24);

  const gpuHeaders = Array.from({ length: GPU_COUNT }, (_, i) => `<th scope="col" class="gpu-column-header" data-gpu="${i}" style="cursor: pointer;" title="Click to select entire column">GPU ${i}</th>`).join("");

  // Get current hour if viewing today
  const { today: todayStr, hour: currentHour } = getServerTimeContext();
  const isViewingToday = state.selectedDay === todayStr;

  const rows = hours
    .map((hour) => {
      const row = rowByHour.get(hour);
      const slotId = row ? row.slot : null;
      const isCurrentHour = isViewingToday && hour === currentHour ? "current-hour" : "";
      const hourCell = `<th scope="row">${hourLabelText(hour)}</th>`;
      const cells = Array.from({ length: GPU_COUNT }, (_, gpu) => {
        const entry = row ? row.entries.find((item) => item.gpu === gpu) : null;
        const price = entry ? entry.price : 0;
        const winner = entry?.winner || "—";
        const entryStatus = entry ? entry.status : status === "open" ? "open" : "locked";
        const isMine = entry ? entry.isMine : false;
        const hasBid = entry ? entry.hasBid : false;
        const key = entry ? `${weekData.week_start}|${row.slot}|${entry.gpu}` : null;

        // Get usage tracking data
        const liveUsers = entry?.live_users || [];
        const mostFrequentUser = entry?.most_frequent_user;
        const mostFrequentNonOwner = entry?.most_frequent_non_owner;
        const actualUser = entry?.actual_user;
        const isCurrentHourSlot = entry?.is_current_hour || false;

        // Determine visual state based on usage
        const classes = ["slot", entryStatus || "locked"];

        // Base class for ownership
        if (isMine) classes.push("mine");
        if (isCurrentHour) classes.push("current-slot");

        // Usage-based coloring (for executing and historical weeks)
        if ((status === "executing" || status === "final") && winner && winner !== "—") {
          if (isCurrentHourSlot && liveUsers.length > 0) {
            // Current hour - show real-time status
            if (liveUsers.includes(winner)) {
              classes.push("slot-owner-using"); // Green - owner is using
            }
          } else if (actualUser !== undefined && actualUser !== null) {
            // Past hour - show historical result
            if (actualUser === winner) {
              classes.push("slot-owner-used"); // Green - owner used it
            } else if (actualUser === null) {
              classes.push("slot-owner-no-show"); // Yellow - owner didn't use
            }
          }
        }

        // Check if THIS SPECIFIC SLOT is in the notification queue
        const notificationQueue = weekData?.outbid_notification_queue || [];
        const fullSlotId = row ? `${weekData.week_start}|${row.slot}|${gpu}` : null;
        const isInQueue = fullSlotId ? notificationQueue.includes(fullSlotId) : false;

        const isOutbid =
          status === "open" &&
          entry &&
          hasBid &&
          entry.winner &&
          entry.winner !== state.session.username &&
          hasActiveNotifications &&
          isInQueue;  // Only flash if in notification queue

        if (isOutbid) classes.push("outbid");
        const canBid = Boolean(allowBidding && entryStatus === "open" && entry);
        const afford = canBid ? canAffordBid(entry, price + 1) : false;

        const canRelease = Boolean(entry?.canRelease && allowReleasing);

        // Build display elements
        let winnerDisplay = "";
        let overlayDisplay = "";

        if (winner !== "—") {
          // Show the slot owner
          winnerDisplay = `<strong>${winner}</strong>`;

          // For current hour in executing week, show live squatters
          if (status === "executing" && isCurrentHourSlot && liveUsers.length > 0) {
            const squatters = liveUsers.filter(u => u !== winner);
            if (squatters.length > 0) {
              overlayDisplay = `<div class="usage-overlay squatter">${squatters.join(", ")}</div>`;
            }
          }
          // For past hours, show most frequent non-owner if they used it significantly
          else if (status === "executing" && actualUser && actualUser !== winner) {
            overlayDisplay = `<div class="usage-overlay squatter">${actualUser}</div>`;
          }
          // Show historical most frequent user as grey overlay when slot is free (not current hour)
          else if (status === "executing" && !isCurrentHourSlot && mostFrequentUser && mostFrequentUser !== winner) {
            overlayDisplay = `<div class="usage-overlay historical">${mostFrequentUser}</div>`;
          }
        } else {
          // No owner - show most frequent user if available
          winnerDisplay = "—";
          if (status === "executing" && mostFrequentUser) {
            overlayDisplay = `<div class="usage-overlay historical">${mostFrequentUser}</div>`;
          }
        }

        // Show next bid price (current price + 1) if bidding is allowed
        const priceDisplay = allowBidding && entry ? `<div class="slot-price">${price + 1}</div>` : "";

        // Show release button if eligible
        const releaseButton = canRelease
          ? `<button class="release-btn" data-week="${weekData.week_start}" data-slot="${slotId}" data-gpu="${gpu}">Release</button>`
          : "";

        return `
          <td class="${classes.join(" ")}" data-slot="${slotId || ''}" data-gpu="${gpu}" ${canBid && afford ? 'data-clickable="true"' : ''}>
            <div class="slot-inner">
              <div class="slot-winner${isMine ? " mine" : ""}${winner !== "—" && !isMine ? " claimed" : ""}">
                ${winnerDisplay}
              </div>
              ${overlayDisplay}
              ${priceDisplay}
              ${releaseButton}
            </div>
          </td>
        `;
      }).join("");
      return `<tr data-hour="${hour}" class="${isCurrentHour}">${hourCell}${cells}</tr>`;
    })
    .join("");
  return `
    <section class="grid-section">
      <table class="grid gpu-grid">
        <thead>
          <tr><th scope="col">Hour (ET)</th>${gpuHeaders}</tr>
        </thead>
        <tbody>
          ${rows}
        </tbody>
      </table>
    </section>
  `;
}

function renderEmptyGrid() {
  return `<p>No week data available.</p>`;
}

function renderLegend() {
  const weekData = getCurrentWeekData();
  const isExecuting = weekData && weekData.status === "executing";

  // Show different legend for executing week (with usage tracking)
  if (isExecuting) {
    return `
      <div class="legend">
        <span><span class="chip mine"></span> Your Slot</span>
        <span><span class="chip slot-owner-using"></span> Owner Active</span>
        <span><span class="chip slot-owner-no-show"></span> Unused</span>
        <span><span class="chip locked"></span> Past</span>
        <span style="color: #888; font-style: italic;">Grey: Most Frequent</span>
        <span style="color: #dc3545; font-weight: 600;">Red: Unauthorized</span>
      </div>
    `;
  }

  // Default legend for bidding week
  return `
    <div class="legend">
      <span><span class="chip mine"></span> Winning</span>
      <span><span class="chip outbid"></span> Outbid</span>
      <span><span class="chip locked"></span> Locked</span>
      <span><span class="chip reserved"></span> Reserved</span>
    </div>
  `;
}

function renderBalances() {
  const u = state.session;
  const remaining = Math.max(0, u.balance - u.committed);
  return `
    <section class="sidebar-section">
      <h2>Balance</h2>
      <ul>
        <li>Remaining credits: <strong>${remaining}</strong></li>
        <li>Credits committed: <strong>${u.committed}</strong></li>
      </ul>
    </section>
  `;
}

function renderMyWeek() {
  const weeks = state.mySummary?.weeks || [];
  if (!weeks.length) {
    return "";
  }
  const blocks = weeks
    .map((week) => {
      const rows = week.slots
        .map(
          (slot) => `
            <tr>
              <td>${slot.slot.replace("T", " ")}</td>
              <td>GPU ${slot.gpu}</td>
              <td>${slot.price} cr</td>
            </tr>
          `
        )
        .join("");
      return `
        <div class="subsection">
          <h3>${formatWeekLabel(week.week_start)} (${formatStatus(week.status)})</h3>
          <table>
            <thead>
              <tr><th>Slot</th><th>GPU</th><th>Credits</th></tr>
            </thead>
            <tbody>${rows || `<tr><td colspan="3">No slots</td></tr>`}</tbody>
          </table>
        </div>
      `;
    })
    .join("");
  return `
    <section class="sidebar-section">
      <h2>My Week</h2>
      ${blocks}
    </section>
  `;
}

function renderMyBids() {
  if (!state.myBids.length) {
    return "";
  }
  const rows = state.myBids
    .map(
      (bid) => `
        <tr>
          <td>${formatDateTime(bid.timestamp)}</td>
          <td>${formatWeekLabel(bid.week)}</td>
          <td>${bid.slot} (GPU ${bid.gpu})</td>
          <td>${bid.price} cr</td>
          <td>${formatBidStatus(bid.status)}</td>
        </tr>
      `
    )
    .join("");
  return `
    <section class="sidebar-section">
      <h2>Recent Bids</h2>
      <table>
        <thead>
          <tr><th>Time</th><th>Week</th><th>Slot</th><th>Price</th><th>Status</th></tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </section>
  `;
}

function renderAdminPanel() {
  const userRows = state.admin.users
    .map(
      (u) => `
        <tr>
          <td>${u.username}</td>
          <td>${u.balance}</td>
          <td>${u.weekly_budget}</td>
          <td>
            <form data-admin-update="${u.username}" style="display: flex; gap: 4px;">
              <input type="number" name="balance_add" placeholder="Add credits" style="width: 100px;" />
              <input type="number" name="weekly_budget" placeholder="New budget" style="width: 100px;" />
              <button type="submit">Update</button>
            </form>
          </td>
        </tr>
      `
    )
    .join("");
  return `
    <section class="sidebar-section">
      <h2>Admin Panel</h2>
      <div class="subsection">
        <h3>Create User</h3>
        <form id="createUserForm" style="display: flex; gap: 8px; flex-direction: column;">
          <input type="text" name="username" placeholder="Username" required />
          <input type="number" name="weekly_budget" placeholder="Weekly budget" min="0" value="30" />
          <button type="submit">Add User</button>
        </form>
      </div>
      <div class="subsection admin-controls">
        <h3>Day Transition</h3>
        <div style="margin-bottom: 12px; padding: 8px; background: #f0f3f8; border-radius: 4px; font-size: 0.85rem;">
          <div id="transitionHourDisplay">Days start at: Loading...</div>
        </div>
        <form id="transitionHourForm" style="display: flex; gap: 8px; flex-direction: column; margin-bottom: 12px;">
          <label style="font-size: 0.9rem;">Hour when day starts (0-23):</label>
          <input type="number" name="transition_hour" placeholder="0" min="0" max="23" required />
          <button type="submit">Update Transition Hour</button>
        </form>
        <p style="font-size: 0.85rem; color: #666; margin: 0;">Days are 24-hour periods starting at this hour. Default is 0 (midnight).</p>
      </div>
      <div class="subsection admin-controls">
        <h3>Cycle Controls</h3>
        <button type="button" id="resetAllDaysBtn" style="background: #dc3545; border-color: #dc3545;">Reset All Days (Wipe & Reinitialize)</button>
      </div>
      <div class="subsection">
        <h3>Bulk Actions</h3>
        <form id="bulkActionsForm" style="display: flex; gap: 8px; flex-direction: column;">
          <input type="number" name="bulk_balance_add" placeholder="Add credits to all" />
          <input type="number" name="bulk_weekly_budget" placeholder="Set weekly budget for all" />
          <button type="submit">Apply to All Users</button>
        </form>
      </div>
      <div class="subsection">
        <h3>Users</h3>
        <table class="admin-table">
          <thead>
            <tr><th>User</th><th>Balance</th><th>Budget</th><th>Actions</th></tr>
          </thead>
          <tbody>${userRows}</tbody>
        </table>
      </div>
    </section>
  `;
}

function renderPasswordChange() {
  return `
    <section class="sidebar-section">
      <h2>Change Password</h2>
      <form id="changePasswordForm" style="display: flex; gap: 8px; flex-direction: column;">
        <input type="password" name="old_password" placeholder="Old password" required />
        <input type="password" name="new_password" placeholder="New password" required />
        <input type="password" name="confirm_password" placeholder="Confirm new password" required />
        <button type="submit">Change Password</button>
      </form>
    </section>
  `;
}

function bindInteractions() {
  document.getElementById("logoutBtn")?.addEventListener("click", onLogout);
  document.getElementById("historyBtn")?.addEventListener("click", showHistoryModal);
  document
    .querySelectorAll(".week-selector button[data-week-type]")
    .forEach((btn) =>
      btn.addEventListener("click", async (ev) => {
        const button = ev.currentTarget;
        const type = button.dataset.weekType;
        const week = button.dataset.week;
        if (!type || !week) {
          return;
        }
        state.selectedWeekType = type;
        state.userSelectedWeekType = true;
        state.selectedWeek = week;
        const days = getWeekDays(week);
        
        // For current week, default to today if it's in this week
        const todayStr = state.today;
        if (type === "current" && todayStr && days.includes(todayStr)) {
          state.selectedDay = todayStr;
        } else {
          state.selectedDay = days[0];
        }
        
        state.userSelectedDay = false;
        await loadWeekDay(week, state.selectedDay);
        startAutoRefresh(); // Restart auto-refresh based on new week selection
        render();
      })
    );
  // Day tabs removed - each day shows only itself
  document.querySelector("[data-action='ack-outbid']")?.addEventListener("click", async (ev) => {
    ev.preventDefault();
    if (!state.selectedWeek) return;

    const currentDay = state.selectedWeek;
    const currentSelectedDay = state.selectedDay;

    try {
      // Call backend to dismiss notifications for this day
      await fetchJson("/api/dismiss-outbid", {
        method: "POST",
        body: JSON.stringify({ day_key: currentDay }),
      });

      // Hide notification banner immediately
      state.outbid.visible = false;
      state.outbid.items = [];

      // Refresh overview to update yellow highlighting (without changing selection)
      state.overview = await fetchJson("/api/overview");
      state.session = state.overview.user;
      updateServerClock(state.overview);

      // Reload the current day data to remove outbid flashing
      await loadWeekDay(currentDay, currentSelectedDay);

      // Re-render
      render();
    } catch (err) {
      console.error("Failed to dismiss notifications:", err);
      alert("Failed to dismiss notifications. Please try again.");
    }
  });
  document.querySelector("[data-action='undo-bid']")?.addEventListener("click", async (ev) => {
    ev.preventDefault();
    if (!state.lastBid) return;

    try {
      await undoBid(state.lastBid);
      state.lastBid = null;
      await quickRefreshAfterBid();
      render();
    } catch (err) {
      alert(err.message);
    }
  });
  if (state.session.role === "admin") {
    document
      .querySelectorAll("form[data-admin-update]")
      .forEach((form) =>
        form.addEventListener("submit", async (ev) => {
          ev.preventDefault();
          const username = ev.currentTarget.dataset.adminUpdate;
          const formData = new FormData(ev.currentTarget);
          const payload = { username };
          const balanceAdd = formData.get("balance_add");
          if (balanceAdd && Number(balanceAdd) !== 0) {
            payload.balance_delta = Number(balanceAdd);
          }
          const budget = formData.get("weekly_budget");
          if (budget && Number(budget) !== 0) {
            payload.weekly_budget = Number(budget);
          }
          if (Object.keys(payload).length === 1) {
            alert("Please enter at least one value.");
            return;
          }
          try {
            await fetchJson("/api/admin/users/update", {
              method: "POST",
              body: JSON.stringify(payload),
            });
            await refreshData();
          } catch (err) {
            alert(err.message);
          }
        })
      );
    document.getElementById("createUserForm")?.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const formData = new FormData(ev.currentTarget);
      const username = formData.get("username");
      const payload = {
        username: username,
        weekly_budget: Number(formData.get("weekly_budget") || 30),
      };
      try {
        await fetchJson("/api/admin/users/create", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        ev.currentTarget.reset();
        await refreshData();
      } catch (err) {
        alert(err.message);
      }
    });
    document.getElementById("transitionHourForm")?.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const formData = new FormData(ev.target);
      const hour = Number(formData.get("transition_hour"));
      if (hour < 0 || hour > 23) {
        alert("Transition hour must be between 0 and 23");
        return;
      }
      if (!confirm(`Change day transition to ${String(hour).padStart(2, '0')}:00? This will affect how days are structured. Current data may need to be cleared.`)) {
        return;
      }
      try {
        const resp = await fetchJson("/api/admin/transition-hour", {
          method: "POST",
          body: JSON.stringify({ transition_hour: hour }),
        });
        await updateTransitionHourDisplay();
        alert(resp.message || "Transition hour updated successfully");
        await refreshData();
      } catch (err) {
        alert(err.message);
      }
    });

    document.getElementById("resetAllDaysBtn")?.addEventListener("click", async () => {
      if (!confirm("WARNING: This will DELETE ALL day data and reinitialize fresh! All bids and history will be lost. Are you sure?")) {
        return;
      }
      if (!confirm("This action cannot be undone. Really wipe everything and start fresh?")) {
        return;
      }
      try {
        const resp = await fetchJson("/api/admin/reset-all-days", {
          method: "POST",
          body: "{}",
        });
        alert(resp.message || "All days reset successfully");
        window.location.reload();
      } catch (err) {
        alert(err.message);
      }
    });
    document.getElementById("bulkActionsForm")?.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const formData = new FormData(ev.currentTarget);
      const balanceAdd = formData.get("bulk_balance_add");
      const weeklyBudget = formData.get("bulk_weekly_budget");
      const payload = {};
      if (balanceAdd && Number(balanceAdd) !== 0) {
        payload.balance_delta = Number(balanceAdd);
      }
      if (weeklyBudget && Number(weeklyBudget) !== 0) {
        payload.weekly_budget = Number(weeklyBudget);
      }
      if (Object.keys(payload).length === 0) {
        alert("Please enter at least one value.");
        return;
      }
      if (!confirm("Apply these changes to ALL users?")) {
        return;
      }
      try {
        await fetchJson("/api/admin/users/bulk-update", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        await refreshData();
      } catch (err) {
        alert(err.message);
      }
    });
  }

  // Password change for all users (admin and regular users)
  document.getElementById("changePasswordForm")?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const formData = new FormData(ev.currentTarget);
    const oldPassword = formData.get("old_password");
    const newPassword = formData.get("new_password");
    const confirmPassword = formData.get("confirm_password");
    if (newPassword !== confirmPassword) {
      alert("New passwords do not match.");
      return;
    }
    try {
      await fetchJson("/api/users/change-password", {
        method: "POST",
        body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
      });
      alert("Password changed successfully.");
    } catch (err) {
      alert(err.message);
    }
  });

  // Setup bulk selection feature
  setupBulkSelection();

  // Setup winner highlight on hover
  setupWinnerHighlight();

  // Setup release button handlers
  setupReleaseButtons();

  // Update transition hour display for admin
  if (state.session?.role === "admin") {
    updateTransitionHourDisplay();
  }
}

function setupReleaseButtons() {
  document.querySelectorAll('.release-btn').forEach(btn => {
    btn.addEventListener('mousedown', (ev) => {
      ev.stopPropagation();
    });
    btn.addEventListener('click', async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();

      const week = btn.dataset.week;
      const slot = btn.dataset.slot;
      const gpu = parseInt(btn.dataset.gpu);

      try {
        await releaseSlot(week, slot, gpu);
        await refreshBalances();
        render();
      } catch (err) {
        alert(err.message);
      }
    });
  });
}

function setupWinnerHighlight() {
  const grid = document.querySelector('.gpu-grid');
  if (!grid) return;

  grid.addEventListener('mouseover', (ev) => {
    const cell = ev.target.closest('td[data-slot][data-gpu]');
    if (!cell) {
      clearWinnerHighlight();
      return;
    }

    // Get the winner from the cell's winner display
    const winnerEl = cell.querySelector('.slot-winner strong');
    if (!winnerEl) {
      clearWinnerHighlight();
      return;
    }

    const winner = winnerEl.textContent.trim();
    if (!winner || winner === '—') {
      clearWinnerHighlight();
      return;
    }

    // Don't highlight your own slots (different visual treatment)
    if (state.session && winner === state.session.username) {
      clearWinnerHighlight();
      return;
    }

    // Highlight all cells with the same winner
    highlightWinner(winner);
  });

  grid.addEventListener('mouseleave', () => {
    clearWinnerHighlight();
  });
}

function highlightWinner(username) {
  clearWinnerHighlight();

  const allCells = document.querySelectorAll('td[data-slot][data-gpu]');
  allCells.forEach(cell => {
    const winnerEl = cell.querySelector('.slot-winner strong');
    if (winnerEl && winnerEl.textContent.trim() === username) {
      cell.classList.add('winner-highlighted');
    }
  });
}

function clearWinnerHighlight() {
  document.querySelectorAll('.winner-highlighted').forEach(el => {
    el.classList.remove('winner-highlighted');
  });
}

function getCurrentWeekData() {
  if (!state.selectedWeek) {
    return null;
  }
  const dayKey = state.selectedDay || getWeekDays(state.selectedWeek)[0];
  if (!dayKey) {
    return null;
  }
  const cacheKey = `${state.selectedWeek}|${dayKey}`;
  return state.weekCache.get(cacheKey);
}

function canAffordBid(entry, newPrice) {
  if (!state.session) return false;
  const remaining = state.session.balance - state.session.committed;
  if (entry.isMine) {
    return remaining >= 1;
  }
  return remaining >= newPrice;
}

async function submitBid(slot, gpu) {
  const payload = { week: state.selectedWeek, slot, gpu: Number(gpu) };
  return await fetchJson("/api/bid", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

async function undoBid(bidInfo) {
  const payload = {
    week: bidInfo.week,
    slot: bidInfo.slot,
    gpu: Number(bidInfo.gpu),
    previousWinner: bidInfo.previousWinner,
    previousPrice: bidInfo.previousPrice,
  };
  return await fetchJson("/api/bid/undo", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

async function releaseSlot(week, slot, gpu) {
  const payload = {
    week: week,
    slot: slot,
    gpu: Number(gpu),
  };
  return await fetchJson("/api/slot/release", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

async function releaseBulk(slots) {
  const payload = {
    slots: slots
  };
  return await fetchJson("/api/slot/release-bulk", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

async function quickRefreshAfterBid() {
  // Fast refresh: only update user balance and current day view
  const sessionResp = await fetchJson("/api/session");
  if (sessionResp.authenticated) {
    state.session = sessionResp.user;
  }

  // Only reload current day
  if (state.selectedWeek && state.selectedDay) {
    const query = new URLSearchParams({ week: state.selectedWeek, day: state.selectedDay });
    const data = await fetchJson(`/api/week?${query.toString()}`);
    const cacheKey = `${state.selectedWeek}|${state.selectedDay}`;
    state.weekCache.set(cacheKey, data);
    updateOutbidSummary(state.selectedWeek);
  }
}

async function refreshBalances() {
  try {
    state.weekCache.clear();
    state.prefetchedWeeks.clear();
    const sessionResp = await fetchJson("/api/overview");
    state.overview = sessionResp;
    state.session = sessionResp.user;
    updateServerClock(state.overview);
    const activeWeekKey = syncWeekSelection();
    if (activeWeekKey) {
      await loadWeekDay(activeWeekKey, state.selectedDay);
    }
    startCountdown();
    await loadPersonalData();
  } catch (err) {
    console.error(err);
    alert(err.message);
  }
}

async function showHistoryModal() {
  try {
    const data = await fetchJson('/api/history/days');
    const days = data.days || [];

    if (days.length === 0) {
      alert('No historical days available yet.');
      return;
    }

    const modal = document.createElement('div');
    modal.style.cssText = 'position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.7); display: flex; align-items: center; justify-content: center; z-index: 1000;';

    const content = document.createElement('div');
    content.style.cssText = 'background: white; padding: 24px; border-radius: 8px; max-width: 400px;';

    const title = document.createElement('h2');
    title.textContent = 'View Past Days';
    title.style.cssText = 'margin-top: 0;';
    content.appendChild(title);

    const label = document.createElement('label');
    label.textContent = 'Select a date:';
    label.style.cssText = 'display: block; margin: 16px 0 8px 0; font-weight: bold;';
    content.appendChild(label);

    // Create date picker
    const datePicker = document.createElement('input');
    datePicker.type = 'date';
    datePicker.style.cssText = 'width: 100%; padding: 8px; font-size: 16px; border: 1px solid #ccc; border-radius: 4px;';

    // Set min/max based on available days
    const dayDates = days.map(d => d.day).sort();
    datePicker.min = dayDates[0];
    datePicker.max = dayDates[dayDates.length - 1];
    datePicker.value = dayDates[dayDates.length - 1]; // Default to most recent

    content.appendChild(datePicker);

    const buttonContainer = document.createElement('div');
    buttonContainer.style.cssText = 'display: flex; gap: 8px; margin-top: 16px;';

    const viewBtn = document.createElement('button');
    viewBtn.textContent = 'View Day';
    viewBtn.style.cssText = 'flex: 1; padding: 10px; cursor: pointer; background: #007bff; color: white; border: none; border-radius: 4px;';
    viewBtn.onclick = async () => {
      const selectedDate = datePicker.value;
      if (selectedDate && dayDates.includes(selectedDate)) {
        modal.remove();
        await viewHistoricalDay(selectedDate);
      } else {
        alert('No data available for this date.');
      }
    };
    buttonContainer.appendChild(viewBtn);

    const closeBtn = document.createElement('button');
    closeBtn.textContent = 'Close';
    closeBtn.style.cssText = 'flex: 1; padding: 10px; cursor: pointer;';
    closeBtn.onclick = () => modal.remove();
    buttonContainer.appendChild(closeBtn);

    content.appendChild(buttonContainer);

    modal.appendChild(content);
    modal.onclick = (e) => {
      if (e.target === modal) modal.remove();
    };

    document.body.appendChild(modal);
  } catch (err) {
    console.error('Failed to load history:', err);
    alert('Failed to load history: ' + err.message);
  }
}

async function viewHistoricalDay(dayKey) {
  try {
    const params = new URLSearchParams({ date: dayKey });
    const data = await fetchJson(`/api/history/day?${params}`);

    // Store in cache and display
    const cacheKey = `${dayKey}|${dayKey}`;
    state.weekCache.set(cacheKey, data);
    state.selectedWeek = dayKey;
    state.selectedDay = dayKey;
    state.selectedWeekType = "history";
    render();
  } catch (err) {
    console.error('Failed to load historical day:', err);
    alert('Failed to load day: ' + err.message);
  }
}

function getWeekDays(weekStart) {
  // In the new day-based system, each "week" is actually just one day
  // Return array with single day for compatibility
  return [weekStart];
}

async function onLoginSubmit(ev) {
  ev.preventDefault();
  const formData = new FormData(ev.target);
  const payload = {
    username: formData.get("username"),
    password: formData.get("password"),
  };
  try {
    await fetchJson("/api/login", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    await loadSession();
  } catch (err) {
    alert(err.message);
  }
}

async function onLogout() {
  await fetchJson("/api/logout", { method: "POST", body: "{}" });
  stopAutoRefresh();
  state.session = null;
  state.serverNow = null;
  state.today = null;
  state.selectedDay = null;
  state.userSelectedDay = false;
  state.selectedWeekType = null;
  state.userSelectedWeekType = false;
  state.weekCache.clear();
  state.prefetchedWeeks.clear();
  state.outbid.items = [];
  state.outbid.visible = false;
  render();
}

async function loadPersonalData() {
  try {
    state.mySummary = await fetchJson("/api/my/summary");
    const bidsResp = await fetchJson("/api/my/bids?limit=50");
    state.myBids = bidsResp.bids || [];
  } catch (err) {
    console.error(err);
    alert(err.message);
  }
}
function formatWeekLabel(weekStart) {
  if (!weekStart) {
    return "—";
  }
  return new Date(`${weekStart}T12:00:00Z`).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "America/New_York"
  });
}

function formatDateTime(isoString) {
  if (!isoString) {
    return "—";
  }
  return new Date(isoString).toLocaleString();
}

// ========== BULK SELECTION FEATURE ==========

let bulkSelectionInitialized = false;

function setupBulkSelection() {
  if (bulkSelectionInitialized) return;

  bulkSelectionInitialized = true;

  document.addEventListener('mousedown', (ev) => {
    if (ev.target.closest('.release-btn')) {
      return;
    }
    const table = ev.target.closest('.gpu-grid');
    if (!table) return;

    const cell = ev.target.closest('td[data-slot][data-gpu]');
    if (cell) {
      onBulkSelectStart(ev, table);
    }
  });

  document.addEventListener('mouseup', onBulkSelectEnd);

  // GPU column header hover and click to select entire column
  document.addEventListener('mouseenter', (ev) => {
    const header = ev.target.closest('.gpu-column-header');
    if (!header) return;

    const gpuIndex = parseInt(header.dataset.gpu, 10);
    if (isNaN(gpuIndex)) return;

    selectEntireGpuColumn(gpuIndex);
  }, true);

  document.addEventListener('mouseleave', (ev) => {
    const header = ev.target.closest('.gpu-column-header');
    if (!header) return;

    clearBulkSelection();
  }, true);

  document.addEventListener('click', async (ev) => {
    const header = ev.target.closest('.gpu-column-header');
    if (!header) return;

    ev.preventDefault();
    ev.stopPropagation();

    const gpuIndex = parseInt(header.dataset.gpu, 10);
    if (isNaN(gpuIndex)) return;

    selectEntireGpuColumn(gpuIndex);
    await executeBulkActionOnSelectedCells();
  });
}


function onBulkSelectStart(ev, table) {
  if (ev.target.closest('.release-btn')) return;
  const cell = ev.target.closest('td[data-slot][data-gpu]');
  if (!cell || ev.button !== 0) return; // Only left click

  ev.preventDefault();

  state.bulkSelect.isSelecting = true;
  state.bulkSelect.startCell = {
    slot: cell.dataset.slot,
    gpu: parseInt(cell.dataset.gpu)
  };
  state.bulkSelect.endCell = state.bulkSelect.startCell;
  state.bulkSelect.cells.clear();

  updateBulkSelection();

  const mouseMoveHandler = (moveEv) => {
    const targetCell = document.elementFromPoint(moveEv.clientX, moveEv.clientY)?.closest('td[data-slot][data-gpu]');
    if (targetCell && state.bulkSelect.isSelecting) {
      state.bulkSelect.endCell = {
        slot: targetCell.dataset.slot,
        gpu: parseInt(targetCell.dataset.gpu)
      };
      updateBulkSelection();
    }
  };

  document.addEventListener('mousemove', mouseMoveHandler);

  const cleanup = () => {
    document.removeEventListener('mousemove', mouseMoveHandler);
    document.removeEventListener('mouseup', cleanup);
  };

  document.addEventListener('mouseup', cleanup, { once: true });
}

function updateBulkSelection() {
  // Clear previous selection highlighting
  document.querySelectorAll('.bulk-selected').forEach(el => el.classList.remove('bulk-selected'));
  clearWinnerHighlight();
  state.bulkSelect.cells.clear();

  if (!state.bulkSelect.startCell || !state.bulkSelect.endCell) return;

  const weekData = getCurrentWeekData();
  if (!weekData) return;

  // Allow bulk selection for:
  // - Open weeks (bidding)
  // - Executing weeks (releasing)
  const isBiddingMode = weekData.status === 'open';
  const isReleasingMode = weekData.status === 'executing';

  if (!isBiddingMode && !isReleasingMode) return;

  // Find all cells in the selection rectangle
  const rows = weekData.rows;
  const startSlot = state.bulkSelect.startCell.slot;
  const endSlot = state.bulkSelect.endCell.slot;
  const startGpu = state.bulkSelect.startCell.gpu;
  const endGpu = state.bulkSelect.endCell.gpu;

  const minGpu = Math.min(startGpu, endGpu);
  const maxGpu = Math.max(startGpu, endGpu);

  // Get slot indices
  const slotIndices = rows.map(r => r.slot);
  const startIdx = slotIndices.indexOf(startSlot);
  const endIdx = slotIndices.indexOf(endSlot);
  const minIdx = Math.min(startIdx, endIdx);
  const maxIdx = Math.max(startIdx, endIdx);

  // Collect winners whose slots we're selecting (for highlighting)
  const affectedWinners = new Set();

  // Select all cells in rectangle
  for (let i = minIdx; i <= maxIdx; i++) {
    const row = rows[i];
    if (!row) continue;

    for (let gpu = minGpu; gpu <= maxGpu; gpu++) {
      const entry = row.entries.find((item) => item.gpu === gpu);
      if (!entry) continue;

      let canSelect = false;

      if (isBiddingMode) {
        // In bidding mode: select biddable slots
        canSelect = entry.status === 'open';
        // Track winners we're bidding against
        if (canSelect && entry.winner && entry.winner !== state.session.username) {
          affectedWinners.add(entry.winner);
        }
      } else if (isReleasingMode) {
        // In releasing mode: select your own future slots
        canSelect = entry.isMine && canReleaseSlot(row.slot);
      }

      if (canSelect) {
        state.bulkSelect.cells.add(`${row.slot}|${gpu}`);

        // Add visual highlight
        const cell = document.querySelector(`td[data-slot="${row.slot}"][data-gpu="${gpu}"]`);
        if (cell) cell.classList.add('bulk-selected');
      }
    }
  }

  // Highlight all slots owned by affected winners
  if (affectedWinners.size > 0) {
    highlightMultipleWinners(Array.from(affectedWinners));
  }
}

function canReleaseSlot(slotKey) {
  // Check if slot is in the future (next hour or later)
  const now = new Date();
  const nextHour = new Date(now);
  nextHour.setMinutes(0, 0, 0);
  nextHour.setHours(nextHour.getHours() + 1);

  const [dayStr, timeStr] = slotKey.split('T');
  const slotDate = new Date(`${dayStr}T${timeStr}`);
  return slotDate >= nextHour;
}

function highlightMultipleWinners(usernames) {
  const allCells = document.querySelectorAll('td[data-slot][data-gpu]');
  allCells.forEach(cell => {
    const winnerEl = cell.querySelector('.slot-winner strong');
    if (winnerEl) {
      const winner = winnerEl.textContent.trim();
      if (usernames.includes(winner)) {
        cell.classList.add('winner-highlighted');
      }
    }
  });
}

async function onBulkSelectEnd(ev) {
  if (!state.bulkSelect.isSelecting) return;

  state.bulkSelect.isSelecting = false;

  const selectedCells = Array.from(state.bulkSelect.cells);

  if (selectedCells.length === 0) {
    clearBulkSelection();
    return;
  }

  const weekData = getCurrentWeekData();
  if (!weekData) {
    clearBulkSelection();
    return;
  }

  const isBiddingMode = weekData.status === 'open';
  const isReleasingMode = weekData.status === 'executing';

  if (isBiddingMode) {
    await handleBulkBid(selectedCells, weekData);
  } else if (isReleasingMode) {
    await handleBulkRelease(selectedCells, weekData);
  }

  clearBulkSelection();
}

async function handleBulkBid(selectedCells, weekData) {
  let totalCost = 0;
  const bids = [];

  for (const cellKey of selectedCells) {
    const [slot, gpuStr] = cellKey.split('|');
    const gpu = parseInt(gpuStr);

    const row = weekData.rows.find(r => r.slot === slot);
    if (!row) continue;

    const entry = row.entries.find((item) => item.gpu === gpu);
    if (!entry || entry.status !== 'open') continue;

    const bidPrice = entry.price + 1;
    totalCost += bidPrice;
    bids.push({ week: state.selectedWeek, slot, gpu, price: bidPrice });
  }

  // Check if user can afford
  const available = state.session.balance - state.session.committed;
  if (totalCost > available) {
    alert(`Cannot afford bulk bid!\n\nTotal cost: ${totalCost} credits\nAvailable: ${available} credits\nShort by: ${totalCost - available} credits`);
    return;
  }

  // Single bid - no prompt, just execute
  if (bids.length === 1) {
    await executeSingleBid(bids[0]);
    return;
  }

  // Multiple bids - prompt for confirmation
  const confirmed = confirm(
    `Bulk Bid Confirmation\n\n` +
    `Slots selected: ${bids.length}\n` +
    `Total cost: ${totalCost} credits\n` +
    `Your available balance: ${available} credits\n\n` +
    `Proceed with bulk bid?`
  );

  if (!confirmed) return;

  // Execute bulk bids atomically
  await executeBulkBids(bids);
}

async function handleBulkRelease(selectedCells, weekData) {
  const slots = [];

  for (const cellKey of selectedCells) {
    const [slot, gpuStr] = cellKey.split('|');
    const gpu = parseInt(gpuStr);

    slots.push({
      week: state.selectedWeek,
      slot: slot,
      gpu: gpu
    });
  }

  if (slots.length === 0) return;

  // Calculate total refund (0.34 per slot, show integer part)
  const totalRefund = slots.length * 0.34;
  const refundDisplay = Math.floor(totalRefund);

  // Single release - no prompt, just execute
  if (slots.length === 1) {
    try {
      await releaseSlot(slots[0].week, slots[0].slot, slots[0].gpu);
      await refreshBalances();
      render();
    } catch (err) {
      alert(err.message);
    }
    return;
  }

  // Multiple releases - show prompt with credit gain
  const confirmed = confirm(
    `Release ${slots.length} slots?\n\n` +
    `You will gain ${refundDisplay} credits.`
  );

  if (!confirmed) return;

  // Execute bulk release
  try {
    await releaseBulk(slots);
    await refreshBalances();
    render();
  } catch (err) {
    alert(err.message);
  }
}

async function executeSingleBid(bid) {
  // Single bid - fast execution, no loading overlay
  // First, capture the current state for potential undo
  const weekData = getCurrentWeekData();
  if (weekData) {
    const row = weekData.rows.find(r => r.slot === bid.slot);
    if (row) {
      const entry = row.entries.find(e => e.gpu === bid.gpu);
      if (entry) {
        const previousWinner = entry.winner;
        const previousPrice = entry.price;

        // Only allow undo if slot was empty OR was already yours
        const canUndo = !previousWinner || previousWinner === state.session.username;

        if (canUndo) {
          state.lastBid = {
            week: bid.week,
            slot: bid.slot,
            gpu: bid.gpu,
            previousWinner: previousWinner,
            previousPrice: previousPrice,
            timestamp: Date.now(),
          };
        } else {
          state.lastBid = null;
        }
      }
    }
  }

  try {
    await submitBid(bid.slot, bid.gpu);
    await quickRefreshAfterBid();
    render();
  } catch (err) {
    console.error(`Failed to bid on ${bid.slot} GPU ${bid.gpu}:`, err);
    alert(err.message);
    state.lastBid = null; // Clear on error
  }
}

async function executeBulkBids(bids) {
  // Clear any pending single-bid undo (bulk bids can't be undone)
  state.lastBid = null;

  // Show loading overlay for bulk operations
  showLoadingOverlay(`Placing ${bids.length} bids atomically...`);

  try {
    // Use the new bulk endpoint for atomic all-or-nothing execution
    const response = await fetchJson("/api/bid/bulk", {
      method: "POST",
      body: JSON.stringify({ bids }),
    });

    // Refresh UI once at the end
    await quickRefreshAfterBid();

    // Hide loading overlay
    hideLoadingOverlay();

    render();

    // Show success message
    if (response.ok) {
      // Success - no need to alert
    }
  } catch (err) {
    console.error('Bulk bid failed:', err);

    // Hide loading overlay
    hideLoadingOverlay();

    // Show error - all-or-nothing approach means either all succeeded or all failed
    alert(`Bulk bid failed!\n\n${err.message}\n\nNo bids were placed.`);

    // Still refresh to show current state
    await quickRefreshAfterBid();
    render();
  }
}

function showLoadingOverlay(message) {
  let overlay = document.getElementById('loading-overlay');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = 'loading-overlay';
    overlay.innerHTML = `
      <div class="loading-content">
        <div class="loading-spinner"></div>
        <div class="loading-message"></div>
      </div>
    `;
    document.body.appendChild(overlay);
  }
  overlay.querySelector('.loading-message').textContent = message;
  overlay.style.display = 'flex';
}

function updateLoadingOverlay(message) {
  const overlay = document.getElementById('loading-overlay');
  if (overlay) {
    overlay.querySelector('.loading-message').textContent = message;
  }
}

function hideLoadingOverlay() {
  const overlay = document.getElementById('loading-overlay');
  if (overlay) {
    overlay.style.display = 'none';
  }
}

function clearBulkSelection() {
  state.bulkSelect.isSelecting = false;
  state.bulkSelect.startCell = null;
  state.bulkSelect.endCell = null;
  state.bulkSelect.cells.clear();
  document.querySelectorAll('.bulk-selected').forEach(el => el.classList.remove('bulk-selected'));
  clearWinnerHighlight();
}

function selectEntireGpuColumn(gpuIndex) {
  const weekData = getCurrentWeekData();
  if (!weekData || !weekData.rows || weekData.rows.length === 0) return;

  // Get first and last slot
  const rows = weekData.rows;
  const firstSlot = rows[0].slot;
  const lastSlot = rows[rows.length - 1].slot;

  // Set bulk selection to span entire column for this GPU
  state.bulkSelect.startCell = {
    slot: firstSlot,
    gpu: gpuIndex
  };
  state.bulkSelect.endCell = {
    slot: lastSlot,
    gpu: gpuIndex
  };

  // Trigger selection update
  updateBulkSelection();
}

async function executeBulkActionOnSelectedCells() {
  const weekData = getCurrentWeekData();
  if (!weekData) {
    clearBulkSelection();
    return;
  }

  const selectedCells = Array.from(state.bulkSelect.cells);

  if (selectedCells.length === 0) {
    clearBulkSelection();
    return;
  }

  const isBiddingMode = weekData.status === 'open';
  const isReleasingMode = weekData.status === 'executing';

  if (isBiddingMode) {
    await handleBulkBid(selectedCells, weekData);
  } else if (isReleasingMode) {
    await handleBulkRelease(selectedCells, weekData);
  }

  clearBulkSelection();
}

