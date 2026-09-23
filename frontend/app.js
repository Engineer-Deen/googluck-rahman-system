/*
  This frontend only ever talks to ITS OWN local Flask server at
  127.0.0.1:5000 -- never directly to the central server. That's
  deliberate: the local server is the thing that's always reachable
  (even offline), and it's the local server's own background worker
  that handles reaching central when possible. The UI never needs to
  know or care whether central is reachable right now.

  Use 127.0.0.1 (not localhost): on Windows, localhost can resolve to
  IPv6 ::1 first while the Tauri sidecar binds IPv4 only.
*/
const API_BASE = "http://127.0.0.1:5000/api";
const LOCAL_BACKEND_READY_TIMEOUT_MS = 45000;
const LOCAL_BACKEND_POLL_MS = 250;
let localBackendReady = false;

let authToken = null;
let currentStaff = null;
localStorage.removeItem("glr_token");
localStorage.removeItem("glr_staff");
let currentMode = "local";
let productsCache = [];
let inventoryEditProductId = null;
let sessionSales = [];
let saleCart = [];
let productsLoadedAt = 0;
let productsLoadPromise = null;
const PRODUCT_CACHE_MS = 2500;
const VOID_REASONS = [
  "Customer returned item",
  "Wrong product or quantity entered",
  "Wrong price entered",
  "Duplicate sale",
  "Payment issue",
  "Customer cancelled order",
  "Other approved reason"
];
const DEFAULT_SYSTEM_SETTINGS = { timeoutMinutes: 15, fullLoginHours: 8, pinConfigured: false };
let systemSettingsCache = Object.assign({}, DEFAULT_SYSTEM_SETTINGS);
let historySearchTimer = null;
let pendingSaleWatchers = new Map();
let reasonResolver = null;
let adminLocked = false;
let adminSecurityTimer = null;
let activityTimer = null;


// ---------- tiny UUID v4, used for client-generated ids (sales, stock
// movements) so records stay idempotent across retries and devices,
// exactly matching the backend's expectations. Prefers the browser's
// built-in generator when available. ----------
function uuidv4() {
  if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

// ---------- login role selector ----------
// This DOES get sent to the server now, but only as a courtesy check
// (see backend/app/routes/auth.py) -- the server independently
// verifies the account's real role against the selected group and
// rejects a mismatch with a clear message. The actual permissions a
// logged-in session gets are always determined by the account's real
// role from the database, never by which button was clicked here.
let selectedLoginRole = "owner";
function setLoginRole(role) {
  selectedLoginRole = role;
  document.querySelectorAll(".role-btn").forEach((b) => b.classList.remove("active"));
  document.querySelector(`.role-btn[data-role="${role}"]`).classList.add("active");
}

// ---------- theme ----------
function setTheme(name) {
  document.documentElement.setAttribute("data-theme", name);
  localStorage.setItem("glr_theme", name);
  document.getElementById("theme-btn-dark").classList.toggle("active", name === "dark");
  document.getElementById("theme-btn-light").classList.toggle("active", name === "light");
}
(function initTheme() {
  setTheme(localStorage.getItem("glr_theme") || "dark");
})();

// ---------- bottom-right toast notifications ----------
let toastTimer = null;
function toast(message, kind) {
  const el = document.getElementById("toast");
  const msg = document.getElementById("toast-message");
  const title = document.getElementById("toast-title");
  const icon = document.getElementById("toast-icon");
  if (!el || !msg) return;
  clearTimeout(toastTimer);
  msg.textContent = message;
  title.textContent = kind === "error" ? "Action needs attention" : kind === "warning" ? "Needs attention" : kind === "success" ? "Completed" : "Notification";
  icon.textContent = kind === "error" ? "!" : kind === "warning" ? "!" : kind === "success" ? "✓" : "i";
  el.className = "toast show" + (kind ? " " + kind : "");
  toastTimer = setTimeout(closeToast, 4000);
}
function closeToast() {
  const el = document.getElementById("toast");
  if (el) el.classList.remove("show");
  clearTimeout(toastTimer);
  toastTimer = null;
}

// Sync toasts only on meaningful transitions; identical retries are suppressed.
let lastSyncToastAt = 0;
let lastSyncToastMsg = "";
function syncToast(message, kind) {
  const now = Date.now();
  if (message === lastSyncToastMsg && now - lastSyncToastAt < 60000) return;
  lastSyncToastMsg = message;
  lastSyncToastAt = now;
  toast(message, kind);
}

// ---------- API helper ----------
async function api(path, options = {}) {
  if (!localBackendReady) {
    await waitForLocalBackend();
  }
  const headers = Object.assign(
    { "Content-Type": "application/json" },
    options.headers || {}
  );
  // Capture at request start so a concurrent 401 from an older unauthenticated
  // poll cannot wipe a token that was set while this request was in flight.
  const tokenUsed = authToken;
  if (tokenUsed) headers["Authorization"] = "Bearer " + tokenUsed;

  let res;
  try {
    res = await fetch(API_BASE + path, Object.assign({}, options, { headers }));
  } catch (_) {
    const error = new Error("The local POS server is unavailable.");
    error.networkFailure = true;
    throw error;
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* no body */ }

  if (!res.ok) {
    if (res.status === 401 && !path.endsWith("/auth/login") && tokenUsed) {
      authToken = null;
      currentStaff = null;
      localStorage.removeItem("glr_token");
      localStorage.removeItem("glr_staff");
      document.getElementById("app").classList.remove("visible");
      document.getElementById("login-overlay").style.display = "flex";
      toast("Your session expired. Please log in again.", "error");
    }
    const rawMessage = (data && data.error) || `Request failed (${res.status})`;
    const message = res.status === 403
      ? rawMessage
      : res.status === 503
        ? (rawMessage && !/^CENTRAL SERVER/i.test(rawMessage)
            ? rawMessage
            : "The online server is temporarily unavailable. You can keep selling offline; try again when the connection returns.")
        : /not enough stock|out of stock/i.test(rawMessage)
          ? rawMessage
          : rawMessage;
    const error = new Error(message);
    error.code = data && data.code;
    if (tokenUsed && (error.code === "central_session_network" || error.code === "central_session_unavailable")) {
      authToken = null;
      currentStaff = null;
      localStorage.removeItem("glr_token");
      localStorage.removeItem("glr_staff");
      document.getElementById("app").classList.remove("visible");
      document.getElementById("login-overlay").style.display = "flex";
      showLoginConnectivityState(
        "Central Server Connection Required",
        "Your session requires a connection to the central server."
      );
    }
    error.authExpired = res.status === 401 && !path.endsWith("/auth/login") && !!tokenUsed;
    throw error;
  }
  return data;
}

// ---------- auth ----------
function togglePasswordVisibility() {
  const input = document.getElementById("login-password");
  const btn = document.querySelector(".password-toggle");
  const showing = input.type === "text";
  input.type = showing ? "password" : "text";
  btn.textContent = showing ? "Show" : "Hide";
}

async function doLogin() {
  const email = document.getElementById("login-email").value.trim();
  const password = document.getElementById("login-password").value;
  const errorEl = document.getElementById("login-error");
  const btn = document.getElementById("login-submit-btn");
  errorEl.textContent = "";
  hideLoginConnectivityState();

  if (!email || !password) {
    errorEl.textContent = "Enter your email and password.";
    return;
  }

  btn.disabled = true;
  btn.textContent = "LOGGING IN...";
  try {
    const data = await api("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password, role_group: selectedLoginRole }),
    });
    authToken = data.token;
    currentStaff = data.staff;
    if (ADMIN_ROLES.includes(currentStaff.role)) {
      localStorage.setItem("glr_admin_session_started", String(Date.now()));
      localStorage.setItem("glr_admin_last_active", String(Date.now()));
    }
    await enterApp();
  } catch (err) {
    if (!navigator.onLine || err.code === "central_auth_network") {
      showLoginConnectivityState(
        "Connection Required",
        "An internet connection to the central server is required to log in."
      );
    } else if (err.code === "central_auth_unavailable" || err.networkFailure) {
      showLoginConnectivityState(
        "Central Server Unavailable",
        "We couldn't reach the central authentication server. Please try again."
      );
    } else {
      errorEl.textContent = err.message;
    }
  } finally {
    btn.disabled = false;
    btn.textContent = "LOG IN";
  }
}

function showLoginConnectivityState(title, message) {
  const state = document.getElementById("login-connectivity-state");
  document.getElementById("login-connectivity-title").textContent = title;
  document.getElementById("login-connectivity-message").textContent = message;
  state.hidden = false;
}

function hideLoginConnectivityState() {
  const state = document.getElementById("login-connectivity-state");
  if (state) state.hidden = true;
}

function retryCentralConnection() {
  hideLoginConnectivityState();
  doLogin();
}

const PROVISIONING_STATE_LABELS = {
  NOT_ENROLLED: "Not enrolled",
  "ENROLLED / PROVISIONING": "Setting up...",
  READY: "Ready",
  SYNC_ERROR: "Sync error - check connection",
};

async function loadProvisioningStatus() {
  const panel = document.getElementById("provisioning-panel");
  const stateEl = document.getElementById("provisioning-state");
  const deviceEl = document.getElementById("provisioning-device");
  const enrollButton = document.getElementById("provision-submit-btn");
  const retryButton = document.getElementById("provision-retry-btn");
  const errorEl = document.getElementById("provisioning-error");
  if (!panel || !stateEl || !deviceEl || !localBackendReady) return;
  try {
    const res = await fetch(API_BASE + "/sync/provisioning/status");
    const data = await res.json();
    const state = data.state || "NOT_ENROLLED";
    stateEl.textContent = PROVISIONING_STATE_LABELS[state] || state;
    deviceEl.textContent = data.device_id ? `Device: ${data.device_id}` : "";
    panel.style.display = state === "READY" ? "none" : "block";

    // Keep the two first-run actions mutually exclusive:
    // enrollment is only for a device that has never been authorized;
    // retry is only for an already-enrolled device whose initial pull failed.
    if (enrollButton) enrollButton.style.display = state === "NOT_ENROLLED" ? "" : "none";
    if (retryButton) retryButton.style.display = state === "SYNC_ERROR" ? "" : "none";

    if (state === "SYNC_ERROR" && data.last_pull_error) {
      if (errorEl) errorEl.textContent = data.last_pull_error;
    } else if (state !== "SYNC_ERROR" && errorEl) {
      errorEl.textContent = "";
    }
  } catch (_) {
    stateEl.textContent = PROVISIONING_STATE_LABELS.SYNC_ERROR;
    panel.style.display = "block";
    if (enrollButton) enrollButton.style.display = "none";
    if (retryButton) retryButton.style.display = "";
  }
}

async function provisionDesktop() {
  const email = document.getElementById("central-enrollment-email").value.trim();
  const password = document.getElementById("central-enrollment-password").value;
  const errorEl = document.getElementById("provisioning-error");
  const button = document.getElementById("provision-submit-btn");
  errorEl.textContent = "";
  if (!email || !password) {
    errorEl.textContent = "Enter central owner/admin credentials.";
    return;
  }

  button.disabled = true;
  button.textContent = "AUTHORIZING...";
  try {
    const provisioned = await fetch(API_BASE + "/sync/provisioning/enroll", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        central_email: email,
        central_password: password,
        name: "Good Luck Rahman Main Device",
        platform: getDesktopPlatformLabel(),
      }),
    });
    const provisionedData = await provisioned.json();
    if (!provisioned.ok) throw new Error(provisionedData.error || "Desktop provisioning failed.");

    document.getElementById("central-enrollment-password").value = "";
    document.getElementById("login-email").value = provisionedData.staff.email;
    document.getElementById("login-password").value = "";
    document.getElementById("provisioning-error").textContent = "Desktop is ready. Sign in with your central credentials.";
    await loadProvisioningStatus();
  } catch (err) {
    errorEl.textContent = err.message || "Desktop provisioning failed.";
  } finally {
    button.disabled = false;
    button.textContent = "AUTHORIZE DESKTOP";
  }
}

async function retryProvisioning() {
  const button = document.getElementById("provision-retry-btn");
  const errorEl = document.getElementById("provisioning-error");
  if (!button) return;

  if (errorEl) errorEl.textContent = "";
  button.disabled = true;
  button.textContent = "RETRYING...";
  try {
    const res = await fetch(API_BASE + "/sync/provisioning/retry", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.error || "Central synchronization could not complete.");
    }

    if (errorEl) errorEl.textContent = "Central synchronization completed. Desktop is ready.";
    await loadProvisioningStatus();
  } catch (err) {
    if (errorEl) errorEl.textContent = err.message || "Central synchronization could not complete.";
    await loadProvisioningStatus();
  } finally {
    button.disabled = false;
    button.textContent = "RETRY CENTRAL SYNC";
  }
}

async function doLogout() {
  try {
    if (authToken) await api("/auth/logout", { method: "POST" });
  } catch (_) {}
  if (syncPollTimer) {
    clearInterval(syncPollTimer);
    syncPollTimer = null;
  }
  lastSyncSnapshot = { key: null, pending: 0 };
  lastSyncToastMsg = "";
  lastSyncToastAt = 0;
  authToken = null;
  currentStaff = null;
  localStorage.removeItem("glr_token");
  localStorage.removeItem("glr_staff");
  localStorage.removeItem("glr_admin_session_started");
  localStorage.removeItem("glr_admin_last_active");
  saleCart = [];
  renderSaleCart();
  document.getElementById("app").classList.remove("visible");
  document.getElementById("login-overlay").style.display = "flex";

  // Move the theme toggle back to its floating position over the login
  // screen, since the header it was living in is no longer visible.
  const toggle = document.getElementById("theme-toggle");
  toggle.classList.remove("inline");
  document.body.appendChild(toggle);
}

async function enterApp() {
  document.getElementById("login-overlay").style.display = "none";
  document.getElementById("app").classList.add("visible");
  document.getElementById("header-staff-name").textContent =
    currentStaff ? `${currentStaff.name} (${currentStaff.role})` : "";

  // Move the theme toggle INTO the header's flex row instead of leaving
  // it fixed-positioned over the top-right corner -- fixed positioning
  // is what caused it to sit on top of the staff name / logout button.
  const toggle = document.getElementById("theme-toggle");
  const headerRight = document.getElementById("header-right");
  toggle.classList.add("inline");
  headerRight.insertBefore(toggle, headerRight.firstChild);

  applyRoleVisibility();
  initializeAdminSecurity();
  refreshAll();
  loadShopBranding();
  if (currentStaff && ADMIN_ROLES.includes(currentStaff.role)) loadSystemSettings();
  startSyncStatusPolling();

  if (authToken && currentStaff) {
    await ensureDeviceRegistration();
  }
}

// ---------- role-based UI ----------
// Purely cosmetic: hides tabs/sections a role shouldn't see. The
// backend enforces every one of these independently (roles_required
// decorators, central-mode guards) -- this never IS the security
// boundary, it just avoids showing someone a button that would 403 if
// they clicked it. Two tiers, matching the backend exactly:
//   finance-only:    owner, admin, manager (product/stock management)
//   admin-only:      owner, admin only (staff management, audit log)
const FINANCE_ROLES = ["owner", "admin", "manager"];
const ADMIN_ROLES = ["owner", "admin"];

function applyRoleVisibility() {
  const role = currentStaff ? currentStaff.role : null;
  const isFinance = FINANCE_ROLES.includes(role);
  const isAdmin = ADMIN_ROLES.includes(role);
  const isOwner = role === "owner";

  document.querySelectorAll(".owner-only").forEach((el) => { el.style.display = isOwner ? "" : "none"; });
  document.querySelectorAll(".finance-only").forEach((el) => {
    el.style.display = isFinance ? "" : "none";
  });
  document.querySelectorAll(".admin-only").forEach((el) => {
    el.style.display = isAdmin ? "" : "none";
  });

  const addProductCard = document.getElementById("add-product-card");
  if (addProductCard) {
    addProductCard.style.display = isFinance ? "" : "none";
  }

  const createStaffCard = document.getElementById("staff-create-card");
  if (createStaffCard) {
    createStaffCard.style.display = isAdmin ? "" : "none";
  }

  const shopSettingsCard = document.getElementById("shop-settings-card");
  if (shopSettingsCard) {
    shopSettingsCard.style.display = isAdmin ? "" : "none";
  }

  const adminRegistrationCard = document.getElementById("admin-registration-card");
  if (adminRegistrationCard) {
    adminRegistrationCard.style.display = isOwner ? "" : "none";
  }
}

// ---------- panels ----------
function showPanel(name) {
  document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
  document.querySelectorAll(".nav-tab").forEach((t) => t.classList.remove("active"));
  document.getElementById("panel-" + name).classList.add("active");
  document.querySelector(`.nav-tab[data-panel="${name}"]`).classList.add("active");
  if (name === "dashboard") loadDashboard();
  if (name === "sales") loadSalesPanel();
  if (name === "payments") loadPaymentDesk();
  if (name === "inventory") loadInventoryPanel();
  if (name === "history") loadHistory("today");
  if (name === "staff") loadStaffPanel();
  if (name === "audit") loadAuditLog();
  if (name === "settings") loadSystemSettings();
}

async function refreshAll() {
  await loadProducts();
  loadDashboard();
}

// ---------- inventory management ----------
async function loadInventoryPanel() {
  try {
    const includeInactive = document.getElementById("show-inactive-toggle")?.checked;
    const products = await api(`/products${includeInactive ? "?include_inactive=true" : ""}`);
    const tbody = document.getElementById("inventory-table");
    const empty = document.getElementById("inventory-empty");

    productsCache = products || [];
    productsLoadedAt = Date.now();

    const restockSelect = document.getElementById("r-product");
    restockSelect.innerHTML = '<option value="">Select product</option>';
    products.forEach((product) => {
      const option = document.createElement("option");
      option.value = product.id;
      option.textContent = `${product.name} (${product.stock})`;
      restockSelect.appendChild(option);
    });

    const canManageCatalog = currentStaff && FINANCE_ROLES.includes(currentStaff.role);

    tbody.innerHTML = "";

    if (products.length === 0) {
      empty.style.display = "block";
      return;
    }

    empty.style.display = "none";

    products.forEach((product) => {
      const tr = document.createElement("tr");
      const actionButtons = canManageCatalog
        ? `
            <button class="btn btn-secondary btn-sm" onclick="editProduct(${product.id})">EDIT</button>
            <button class="btn btn-${product.is_active ? "danger" : "success"} btn-sm" onclick="toggleInventoryProductState(${product.id}, ${!product.is_active})">${product.is_active ? "DEACTIVATE" : "REACTIVATE"}</button>
          `
        : '<span class="muted">Sync-managed</span>';
      tr.innerHTML = `
        <td style="font-family:'DM Mono',monospace;font-size:0.78rem;">${escapeHtml(product.sku || "-")}</td>
        <td>${escapeHtml(product.name)}</td>
        <td>${escapeHtml(product.category)}</td>
        <td>${money(product.unit_price)}</td>
        <td>${product.stock}</td>
        <td class="action-cell">${actionButtons}</td>
      `;
      tbody.appendChild(tr);
    });
  } catch (err) {
    toast(err.message, "error");
  }
}

function clearProductForm() {
  inventoryEditProductId = null;
  document.getElementById("product-form-title").textContent = "Add New Product";
  document.getElementById("p-sku").value = "";
  document.getElementById("p-name").value = "";
  document.getElementById("p-category").value = "";
  document.getElementById("p-unit-price").value = "";
  document.getElementById("p-cost-price").value = "";
  document.getElementById("p-cancel-btn").style.display = "none";
}

async function editProduct(productId) {
  try {
    const product = await api(`/products/${productId}`);
    inventoryEditProductId = productId;
    document.getElementById("product-form-title").textContent = "Edit Product";
    document.getElementById("p-sku").value = product.sku || "";
    document.getElementById("p-name").value = product.name || "";
    document.getElementById("p-category").value = product.category || "";
    document.getElementById("p-unit-price").value = product.unit_price || "";
    document.getElementById("p-cost-price").value = product.cost_price || "";
    document.getElementById("p-cancel-btn").style.display = "inline-block";
    document.getElementById("add-product-card")?.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    toast(err.message, "error");
  }
}

async function submitProductForm() {
  const name = document.getElementById("p-name").value.trim();
  const category = document.getElementById("p-category").value;
  const unitPrice = Number(document.getElementById("p-unit-price").value || 0);
  const costPrice = Number(document.getElementById("p-cost-price").value || 0);

  if (!name) {
    toast("Product name is required.", "error");
    return;
  }
  if (!category) {
    toast("Please select a valid product category.", "error");
    return;
  }

  try {
    if (inventoryEditProductId) {
      const reason = await openReasonModal("Why are you changing this product?");
      if (!reason) return;
      await api(`/products/${inventoryEditProductId}`, {
        method: "PUT",
        body: JSON.stringify({
          name,
          category,
          unit_price: unitPrice,
          cost_price: costPrice,
          reason,
        }),
      });
      toast("Product updated.", "success");
    } else {
      await api("/products", {
        method: "POST",
        body: JSON.stringify({
          name,
          category,
          unit_price: unitPrice,
          cost_price: costPrice,
        }),
      });
      toast("Product created.", "success");
    }

    clearProductForm();
    await loadProducts(true);
    await loadInventoryPanel();
  } catch (err) {
    toast(err.message, "error");
  }
}

async function toggleInventoryProductState(productId, makeActive) {
  try {
    const reason = await openReasonModal(makeActive ? "Why are you reactivating this product?" : "Why are you deactivating this product?");
    if (!reason) return;
    await api(`/products/${productId}`, {
      method: "PUT",
      body: JSON.stringify({
        is_active: makeActive,
        reason,
      }),
    });
    toast(makeActive ? "Product reactivated." : "Product deactivated.", "success");
    await loadProducts(true);
    await loadInventoryPanel();
  } catch (err) {
    toast(err.message, "error");
  }
}

async function submitRestock() {
  const productId = Number(document.getElementById("r-product").value);
  const qty = Number(document.getElementById("r-qty").value || 0);
  const reason = document.getElementById("r-reason").value;

  if (!productId) {
    toast("Select a product first.", "error");
    return;
  }
  if (!Number.isInteger(qty) || qty === 0) {
    toast("Enter a valid whole-number quantity change.", "error");
    return;
  }

  try {
    await api("/stock-movements", {
      method: "POST",
      body: JSON.stringify({
        product_id: productId,
        quantity_delta: qty,
        reason,
      }),
    });
    toast("Stock updated.", "success");
    document.getElementById("r-product").value = "";
    document.getElementById("r-qty").value = "";
    await loadProducts(true);
    await loadInventoryPanel();
  } catch (err) {
    toast(err.message, "error");
  }
}

// ---------- products (shared cache) ----------
async function loadProducts(force = false) {
  const now = Date.now();
  if (!force && productsCache.length && now - productsLoadedAt < PRODUCT_CACHE_MS) return productsCache;
  if (productsLoadPromise && !force) return productsLoadPromise;
  productsLoadPromise = api("/products").then((data) => {
    productsCache = data || [];
    productsLoadedAt = Date.now();
    return productsCache;
  }).catch((err) => {
    toast(err.message, "error");
    return productsCache;
  }).finally(() => { productsLoadPromise = null; });
  return productsLoadPromise;
}

function money(n) {
    return "NLe " + Number(n).toFixed(2);
}
function formatSaleProducts(items) {
  if (!items || !items.length) return "—";
  return items.map(i => i.product_name || `Product #${i.product_id}`).join(", ");
}

function getSaleItemCount(items) {
  if (!items || !items.length) return 0;
  return items.reduce((total, item) => total + Number(item.quantity || 0), 0);
}

  // ---------- dashboard ----------
async function loadDashboard() {
  try {
    const [, sales] = await Promise.all([loadProducts(), api("/sales")]);
    const today = new Date().toDateString();
    const todaySales = sales.filter((s) => s.status !== "voided" && new Date(s.created_at).toDateString() === today);

    document.getElementById("stat-sales-count").textContent = todaySales.length;
    const revenue = todaySales.reduce((sum, s) => sum + Number(s.total_amount), 0);
    document.getElementById("stat-revenue").textContent = money(revenue);
    document.getElementById("stat-products").textContent = productsCache.length;
    document.getElementById("stat-low-stock").textContent =
      productsCache.filter((p) => p.stock <= 0).length;

    const outstanding = sales.filter(s => s.status !== "voided").reduce((sum, s) => sum + Number(s.balance || 0), 0);
    document.getElementById("stat-outstanding").textContent = money(outstanding);

    // "profit" only appears in the API response for finance roles (owner/
    // admin/manager) -- its presence, not the local staff object, is what
    // decides whether to show this card, since that's the same rule the
    // server already enforces.
    if (todaySales.some((s) => "profit" in s)) {
      const profitToday = todaySales.reduce((sum, s) => sum + Number(s.profit || 0), 0);
      document.getElementById("stat-profit").textContent = money(profitToday);
      document.getElementById("stat-profit-card").style.display = "block";
    } else {
      document.getElementById("stat-profit-card").style.display = "none";
    }

    const tbody = document.getElementById("dash-recent-sales");
    tbody.innerHTML = "";
    sales.slice(0, 10).forEach((s) => {
      const productSummary = formatSaleProducts(s.items);
      const itemCount = getSaleItemCount(s.items);
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td style="font-family:'DM Mono',monospace;font-size:0.78rem;">${s.invoice_number || "PENDING SYNC"}</td>
        <td>${new Date(s.created_at).toLocaleTimeString()}</td>
        <td>${s.customer_name || "Walk-in"}</td>
        <td>${productSummary}</td>
        <td>${itemCount}</td>
        <td>${money(s.total_amount)}</td>
        ${"profit" in s ? `<td>${money(s.profit)}</td>` : ""}
        <td>${statusTag(s.status)}</td>
        <td class="action-cell"><button class="btn btn-secondary btn-sm" onclick="viewSale('${s.id}')">VIEW</button> ${voidActionCell(s)}</td>
      `;
      tbody.appendChild(tr);
    });
  } catch (err) {
    toast(err.message, "error");
  }
}

function statusTag(status) {
  if (status === "voided") return '<span class="tag tag-danger">voided</span>';
  return status === "completed"
    ? '<span class="tag tag-ok">completed</span>'
    : '<span class="tag tag-warn">incomplete</span>';
}

// Returns the void button's HTML for a sale row, or an empty string if
// it's already voided. The backend is the real gatekeeper for WHO can
// void WHAT (same-day + own-sale for a cashier, anytime for finance
// roles) -- this button is shown to everyone with an active sale and
// simply lets the server's own answer (a clear error toast) explain it
// if someone taps it without permission, rather than duplicating that
// same-day/ownership logic here and risking it drifting out of sync
// with the real rule.
function saleCanEdit(sale) {
  if (!currentStaff) return false;
  if (ADMIN_ROLES.includes(currentStaff.role) || FINANCE_ROLES.includes(currentStaff.role)) return true;
  if (sale.staff_id !== currentStaff.id) return false;
  return new Date(sale.created_at).toDateString() === new Date().toDateString();
}

function voidActionCell(sale) {
  if (sale.status === "voided") return '<span class="muted">—</span>';
  const edit = saleCanEdit(sale) ? `<button class="btn btn-secondary btn-sm" onclick="editSale('${sale.id}')">EDIT</button>` : "";
  const del = `<button class="btn btn-danger btn-sm" onclick="voidSale('${sale.id}')">DELETE</button>`;
  return `${edit} ${del}`;
}

function openReasonModal(title, reasons = VOID_REASONS) {
  return new Promise((resolve) => {
    reasonResolver = resolve;
    document.getElementById("reason-modal-title").textContent = title;
    const select = document.getElementById("reason-modal-select");
    select.innerHTML = '<option value="">Select a reason</option>' + reasons.map(r => `<option value="${r}">${r}</option>`).join("");
    document.getElementById("reason-modal").style.display = "flex";
    setTimeout(() => select.focus(), 50);
  });
}
function submitReasonModal() {
  const value = document.getElementById("reason-modal-select").value;
  if (!value) { toast("Please select a reason.", "error"); return; }
  closeReasonModal(value);
}
function closeReasonModal(value) {
  document.getElementById("reason-modal").style.display = "none";
  if (reasonResolver) { const resolve = reasonResolver; reasonResolver = null; resolve(value); }
}
async function editSale(saleId) {
  try {
    const sale = await api(`/sales/${saleId}`);
    const customer = prompt("Customer name:", sale.customer_name || "");
    if (customer === null) return;
    let items = sale.items.map(i => ({product_id:i.product_id, quantity:i.quantity, unit_price:Number(i.unit_price)}));
    if (sale.items.length === 1) {
      const q = prompt("Correct quantity:", String(items[0].quantity));
      if (q === null) return;
      const price = prompt("Correct selling price:", String(items[0].unit_price));
      if (price === null) return;
      items[0].quantity = Number(q); items[0].unit_price = Number(price);
    }
    const reason = await openReasonModal("Why are you correcting this sale?", ["Wrong product or quantity entered", "Wrong price entered", "Customer information correction", "Payment correction", "Other approved reason"]);
    if (!reason) return;
    await api(`/sales/${saleId}`, { method: "PUT", body: JSON.stringify({ customer_name: customer.trim(), payment_method: sale.payment_method, items, reason }) });
    toast("Sale updated and audit recorded.", "success");
    await loadProducts();
    await loadDashboard();
    await loadHistory(currentHistoryPeriod);
  } catch (err) { toast(err.message, "error"); }
}

async function voidSale(saleId) {
  const reason = await openReasonModal("Why are you deleting/voiding this sale?");
  if (!reason) return;
  try {
    const result = await api(`/sales/${saleId}/void`, {
      method: "POST",
      body: JSON.stringify({ id: uuidv4(), reason }),
    });
    toast("Sale voided. Stock has been restored.", "success");

    // The session table (Sales Entry tab) is a client-only list that
    // never refetches from the server, so it needs its own copy of
    // this sale updated directly -- otherwise it would keep showing
    // the old pre-void status until the page reloads.
    const idx = sessionSales.findIndex((s) => s.id === saleId);
    if (idx !== -1) sessionSales[idx] = result;

    loadDashboard();
    if (document.getElementById("panel-payments").classList.contains("active")) loadPaymentDesk();
    if (document.getElementById("panel-sales").classList.contains("active")) renderSessionTable();
  } catch (err) {
    toast(err.message, "error");
  }
}

// ---------- sales entry ----------
async function loadSalesPanel() {
  await loadProducts();
  const select = document.getElementById("s-product");
  select.innerHTML = '<option value="">Select product</option>';
  // Out-of-stock products don't appear here at all -- there's nothing
  // to sell, so nothing to pick. The backend independently enforces
  // this too (rejects the sale outright if requested quantity exceeds
  // current stock); this is just the matching UI-side courtesy so a
  // seller never gets that far in the first place.
  productsCache.filter((p) => p.stock > 0).forEach((p) => {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = p.name;
    opt.dataset.price = p.unit_price;
    opt.dataset.stock = p.stock;
    select.appendChild(opt);
  });
  renderSessionTable();
}

function getSaleCartTotal(items = saleCart) {
  return items.reduce((total, item) => {
    return total + (Number(item.unit_price) || 0) * (Number(item.quantity) || 0);
  }, 0);
}

function getCurrentSaleDraft() {
  const productId = Number(document.getElementById("s-product").value);
  const price = Number(document.getElementById("s-price").value);
  const qty = Number(document.getElementById("s-qty").value);
  const hasDraftInput = !!document.getElementById("s-product").value ||
    document.getElementById("s-price").value !== "" ||
    document.getElementById("s-qty").value !== "1";

  return { productId, price, qty, hasDraftInput };
}

function renderSaleCart() {
  const tbody = document.getElementById("sale-cart-table");
  const empty = document.getElementById("sale-cart-empty");
  if (!tbody || !empty) return;

  tbody.innerHTML = "";
  empty.style.display = saleCart.length ? "none" : "block";

  saleCart.forEach((item, index) => {
    const product = productsCache.find((p) => Number(p.id) === Number(item.product_id));
    const productName = product ? product.name : `Product #${item.product_id}`;
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(productName)}</td>
      <td>${money(item.unit_price)}</td>
      <td>${Number(item.quantity)}</td>
      <td>${money(Number(item.unit_price) * Number(item.quantity))}</td>
      <td class="action-cell"><button class="btn btn-secondary btn-sm" type="button" onclick="removeSaleItem(${index})">REMOVE</button></td>
    `;
    tbody.appendChild(tr);
  });
}

function addSaleItem() {
  const { productId, price, qty } = getCurrentSaleDraft();
  const select = document.getElementById("s-product");
  const opt = select.selectedOptions[0];

  if (!productId) { toast("Select a product first.", "error"); return; }
  if (!price || price <= 0) { toast("Enter a valid price.", "error"); return; }
  if (!qty || qty <= 0 || !Number.isInteger(qty)) {
    toast("Enter a valid whole-number quantity.", "error");
    return;
  }

  const stock = Number(opt && opt.dataset ? opt.dataset.stock : 0);
  const alreadyInCart = saleCart
    .filter((item) => Number(item.product_id) === productId)
    .reduce((sum, item) => sum + Number(item.quantity || 0), 0);
  if (Number.isFinite(stock) && stock >= 0 && alreadyInCart + qty > stock) {
    toast(`Not enough stock. Only ${Math.max(stock - alreadyInCart, 0)} available for this product.`, "error");
    return;
  }

  saleCart.push({
    id: uuidv4(),
    product_id: productId,
    quantity: qty,
    unit_price: price,
  });
  renderSaleCart();

  // The current line has now been added. Clear only the line-entry controls;
  // customer/payment information belongs to the whole sale and stays intact.
  select.value = "";
  document.getElementById("s-price").value = "";
  document.getElementById("s-qty").value = "1";
  document.getElementById("s-stock-card").style.display = "none";
  calcSaleTotal();
}

function removeSaleItem(index) {
  if (!Number.isInteger(index) || index < 0 || index >= saleCart.length) return;
  saleCart.splice(index, 1);
  renderSaleCart();
  calcSaleTotal();
}

function onPaidInFullToggle() {
  const checked = document.getElementById("s-paid-in-full").checked;
  const amountField = document.getElementById("s-amount-paid");
  if (checked) {
    amountField.value = getSaleCartTotal() === 0 ? "" : getSaleCartTotal().toFixed(2);
    amountField.disabled = true;
  } else {
    amountField.disabled = false;
  }
  calcSaleTotal();
}

function onSaleProductChange() {
  const select = document.getElementById("s-product");
  const opt = select.selectedOptions[0];
  if (!opt || !opt.value) {
    document.getElementById("s-stock-card").style.display = "none";
    calcSaleTotal();
    return;
  }
  document.getElementById("s-price").value = opt.dataset.price;
  document.getElementById("s-stock-count").textContent = opt.dataset.stock;
  document.getElementById("s-stock-card").style.display = "flex";
  calcSaleTotal();
}

function calcSaleTotal() {
  const { productId, price, qty } = getCurrentSaleDraft();
  let total = getSaleCartTotal();
  if (productId && price > 0 && qty > 0) {
    total += price * qty;
  }
  document.getElementById("s-total-disp").textContent = money(total);

  // If "Paid in full" is checked, keep the amount field tracking the
  // complete sale total, including all cart items and the current draft.
  if (document.getElementById("s-paid-in-full").checked) {
    document.getElementById("s-amount-paid").value = total > 0 ? total.toFixed(2) : "";
  }

  const paidField = document.getElementById("s-amount-paid").value;
  const balancePreview = document.getElementById("s-balance-preview");
  if (paidField === "" || total <= 0) {
    balancePreview.style.display = "none";
    return;
  }
  const paid = Number(paidField) || 0;
  const balance = Math.max(total - paid, 0);
  if (balance > 0) {
    document.getElementById("s-balance-disp").textContent = money(balance);
    balancePreview.style.display = "block";
  } else {
    balancePreview.style.display = "none";
  }
}

function formatPersonName(value) {
  return String(value || "").trim().replace(/\s+/g, " ").split(" ").filter(Boolean).map(word => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase()).join(" ");
}
function normalizeInputName(input) {
  if (!input) return;
  const formatted = formatPersonName(input.value);
  if (formatted) input.value = formatted;
}

function clearSaleForm() {
  document.getElementById("s-customer").value = "";
  document.getElementById("s-product").value = "";
  document.getElementById("s-price").value = "";
  document.getElementById("s-qty").value = "1";
  document.getElementById("s-amount-paid").value = "";
  document.getElementById("s-amount-paid").disabled = false;
  document.getElementById("s-paid-in-full").checked = false;
  document.getElementById("s-stock-card").style.display = "none";
  document.getElementById("s-balance-preview").style.display = "none";
  saleCart = [];
  renderSaleCart();
  calcSaleTotal();
}

async function saveSale() {
  const customerInput = document.getElementById("s-customer");
  normalizeInputName(customerInput);
  const customer = customerInput.value.trim();
  const amountPaidField = document.getElementById("s-amount-paid").value;
  const btn = document.getElementById("s-save-btn");
  const draft = getCurrentSaleDraft();
  const finalItems = saleCart.map((item) => ({ ...item }));

  // Preserve the original single-item workflow as well: pressing SAVE SALE
  // with a product entered but not explicitly added to the cart includes that
  // draft automatically. The cart button remains useful for multi-item sales.
  if (draft.hasDraftInput) {
    if (!draft.productId) { toast("Select a product first.", "error"); return; }
    if (!draft.price || draft.price <= 0) { toast("Enter a valid price.", "error"); return; }
    if (!draft.qty || draft.qty <= 0 || !Number.isInteger(draft.qty)) {
      toast("Enter a valid whole-number quantity.", "error");
      return;
    }

    const opt = document.getElementById("s-product").selectedOptions[0];
    const stock = Number(opt && opt.dataset ? opt.dataset.stock : 0);
    const alreadyInCart = saleCart
      .filter((item) => Number(item.product_id) === draft.productId)
      .reduce((sum, item) => sum + Number(item.quantity || 0), 0);
    if (Number.isFinite(stock) && stock >= 0 && alreadyInCart + draft.qty > stock) {
      toast(`Not enough stock. Only ${Math.max(stock - alreadyInCart, 0)} available for this product.`, "error");
      return;
    }

    finalItems.push({
      id: uuidv4(),
      product_id: draft.productId,
      quantity: draft.qty,
      unit_price: draft.price,
    });
  }

  if (!customer) { toast("Enter customer name.", "error"); return; }
  if (!finalItems.length) { toast("Add at least one item to the cart.", "error"); return; }

  const total = finalItems.reduce((sum, item) => sum + Number(item.unit_price) * Number(item.quantity), 0);
  if (!Number.isFinite(total) || total <= 0) { toast("Sale total must be greater than zero.", "error"); return; }
  if (amountPaidField !== "" && Number(amountPaidField) > total) {
    toast("Amount paid cannot exceed the sale total.", "error");
    return;
  }

  // Generated HERE, at the moment of sale, on this device -- not by the
  // server. This makes the sale idempotent and safe to sync later.
  const saleId = uuidv4();

  const payload = {
    id: saleId,
    customer_name: customer,
    items: finalItems,
  };
  // Leaving Amount Paid blank means nothing has been paid yet -- the
  // server defaults it to 0 (an open balance), not a full payment.
  if (amountPaidField !== "") {
    payload.amount_paid = Number(amountPaidField);
  }

  btn.disabled = true;
  btn.textContent = "SAVING...";
  try {
    const sale = await api("/sales", {
      method: "POST",
      body: JSON.stringify(payload),
    });

    const syncPending = !sale.invoice_number;
    if (sale.stock_warning) {
      toast(`${syncPending ? "Sale saved locally" : "Sale completed"}, but stock is now negative. Restock needed.`, "warning");
    } else if (sale.status === "incomplete") {
      toast(`${syncPending ? "Sale saved locally" : "Sale completed"}. Balance of ${money(sale.balance)} still owed.${syncPending ? " Waiting for synchronization." : ""}`, "success");
    } else if (syncPending) {
      toast("Sale saved locally. Waiting for synchronization.", "success");
    } else {
      toast("Sale completed and saved.", "success");
    }

    sessionSales.unshift(sale);
    clearSaleForm();
    if (!sale.invoice_number) watchSaleSync(saleId);
    await loadProducts(true);
    await loadSalesPanel();
    await loadDashboard();
  } catch (err) {
    if (err.authExpired) return;
    toast(err.networkFailure
      ? "The local POS server is unavailable. The sale status is unknown. Check Transaction History before retrying."
      : `Sale failed. It was not completed. ${err.message}`, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "SAVE SALE";
  }
}

function watchSaleSync(saleId) {
  if (!saleId || pendingSaleWatchers.has(saleId)) return;
  let attempts = 0;
  const timer = setInterval(async () => {
    attempts += 1;
    try {
      const sale = await api(`/sales/${saleId}`);
      if (sale.invoice_number) {
        clearInterval(timer);
        pendingSaleWatchers.delete(saleId);
        const idx = sessionSales.findIndex(s => s.id === saleId);
        if (idx >= 0) sessionSales[idx] = sale;
        renderSessionTable();
        await Promise.all([loadDashboard(), loadPaymentDesk()]);
          toast(`Sale synchronized. Invoice ${sale.invoice_number} assigned.`, "success");
      }
    } catch (_) {}
    if (attempts >= 15) { clearInterval(timer); pendingSaleWatchers.delete(saleId); }
  }, 2000);
  pendingSaleWatchers.set(saleId, timer);
}

function renderSessionTable() {
  const tbody = document.getElementById("session-table");
  tbody.innerHTML = "";
  if (sessionSales.length === 0) {
    tbody.innerHTML = '<tr><td colspan="9" class="empty-state">No sales recorded yet this session.</td></tr>';
    return;
  }
  sessionSales.forEach((s) => {
    const item = s.items[0] || {};
    const product = s.items.map(i => i.product_name || productsCache.find(p => p.id === i.product_id)?.name || `Product #${i.product_id}`).join(", ");
    const tr = document.createElement("tr");
    tr.innerHTML = `<td style="font-family:'DM Mono',monospace;font-size:.78rem;">${escapeHtml(s.invoice_number || "PENDING SYNC")}</td><td>${new Date(s.created_at).toLocaleTimeString()}</td><td>${escapeHtml(s.customer_name || "Walk-in")}</td><td>${escapeHtml(product)}</td><td>${s.items.reduce((n,i)=>n+Number(i.quantity||0),0)}</td><td>${money(s.total_amount)}</td>${"profit" in s ? `<td>${money(s.profit)}</td>` : ""}<td>${statusTag(s.status)}</td><td class="action-cell"><button class="btn btn-secondary btn-sm" onclick="viewSale('${s.id}')">VIEW</button> ${voidActionCell(s)}</td>`;
    tbody.appendChild(tr);
  });
}

// ---------- payment desk ----------
let openBalanceSales = [];
let selectedPaymentSaleId = null;

async function loadPaymentDesk() {
  try {
    const sales = await api("/sales?status=incomplete&limit=200");
    openBalanceSales = sales;
    renderPaymentDeskList();
  } catch (err) {
    toast(err.message, "error");
  }
  cancelPayment();
}

function renderPaymentDeskList() {
  const search = document.getElementById("pd-search").value.trim().toLowerCase();
  const filtered = search
    ? openBalanceSales.filter(
        (s) =>
          s.id.toLowerCase().includes(search) ||
          (s.customer_name || "").toLowerCase().includes(search)
      )
    : openBalanceSales;

  const tbody = document.getElementById("pd-table");
  const emptyEl = document.getElementById("pd-empty");
  tbody.innerHTML = "";

  if (filtered.length === 0) {
    emptyEl.style.display = "block";
    return;
  }
  emptyEl.style.display = "none";

  filtered.forEach((s) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td style="font-family:'DM Mono',monospace;font-size:0.78rem;">${s.invoice_number || "PENDING SYNC"}</td>
      <td>${s.customer_name || "Walk-in"}</td>
      <td>${money(s.total_amount)}</td>
      <td>${money(s.amount_paid)}</td>
      <td style="color:var(--warning);">${money(s.balance)}</td>
      ${"profit" in s ? `<td>${money(s.profit)}</td>` : ""}
      <td class="action-cell"><button class="btn btn-primary btn-sm" onclick="selectSaleForPayment('${s.id}')">PAY</button> <button class="btn btn-secondary btn-sm" onclick="viewSale('${s.id}')">VIEW</button> ${voidActionCell(s)}</td>
    `;
    tbody.appendChild(tr);
  });
}

function selectSaleForPayment(saleId) {
  const sale = openBalanceSales.find((s) => s.id === saleId);
  if (!sale) return;
  selectedPaymentSaleId = saleId;
  document.getElementById("pd-pay-sale-id").textContent = sale.invoice_number || "PENDING SYNC";
  document.getElementById("pd-pay-balance").textContent = money(sale.balance);
  document.getElementById("pd-pay-amount").value = sale.balance;
  document.getElementById("pd-pay-error").textContent = "";
  document.getElementById("pd-pay-card").style.display = "block";
  if (!sale.invoice_number) watchSaleSync(sale.id);
}

function cancelPayment() {
  selectedPaymentSaleId = null;
  document.getElementById("pd-pay-card").style.display = "none";
  document.getElementById("pd-pay-error").textContent = "";
}

async function submitPayment() {
  if (!selectedPaymentSaleId) return;

  const amount = Number(document.getElementById("pd-pay-amount").value);
  const errorEl = document.getElementById("pd-pay-error");
  const btn = document.getElementById("pd-pay-submit-btn");
  errorEl.textContent = "";

  if (!amount || amount <= 0) {
    errorEl.textContent = "Enter a valid payment amount.";
    return;
  }

  btn.disabled = true;
  btn.textContent = "RECORDING...";
  try {
    await api(`/sales/${selectedPaymentSaleId}/payments`, {
      method: "POST",
      body: JSON.stringify({ id: uuidv4(), amount }),
    });
    toast("Payment recorded.", "success");
    cancelPayment();
    await Promise.all([loadPaymentDesk(), loadDashboard(), loadSalesPanel()]);

  } catch (err) {
    // A 503 here means "this device needs to be online to pay down a
    // balance" -- see backend/app/routes/sales.py for why that's a
    // deliberate safety rule, not a bug. err.message already carries
    // that exact explanation from the server.
    if (err.authExpired) return;
    errorEl.textContent = err.networkFailure
      ? "Payment status is unknown. Check the sale before trying again."
      : `PAYMENT FAILED: ${err.message}`;
    toast(errorEl.textContent, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "RECORD PAYMENT";
  }
}

// ---------- staff management ----------
let staffCache = [];
let resetPasswordStaffId = null;
let editingStaffId = null;

async function loadStaffPanel() {
  try {
    staffCache = await api("/staff");
  } catch (err) {
    toast(err.message, "error");
    return;
  }

  const tbody = document.getElementById("staff-table");
  tbody.innerHTML = "";
  staffCache.forEach((s) => {
    const tr = document.createElement("tr");
    const statusBadge = s.is_active
      ? '<span class="tag tag-ok">active</span>'
      : '<span class="tag tag-danger">deactivated</span>';
    const toggleLabel = s.is_active ? "DEACTIVATE" : "REACTIVATE";
    const canManageStaff = currentStaff && ADMIN_ROLES.includes(currentStaff.role);
    const canManageThisStaff = canManageStaff && !["owner", "admin"].includes(s.role);
    const actions = canManageStaff
      ? canManageThisStaff
        ? `
          <button class="btn btn-secondary" style="padding:0.35rem 0.7rem;font-size:0.72rem;" onclick="startStaffEdit(${s.id})">EDIT</button>
          <button class="btn btn-secondary" style="padding:0.35rem 0.7rem;font-size:0.72rem;" onclick="startPasswordReset(${s.id})">RESET PW</button>
          <button class="btn btn-secondary" style="padding:0.35rem 0.7rem;font-size:0.72rem;" onclick="toggleStaffActive(${s.id}, ${!s.is_active})">${toggleLabel}</button>
        `
        : '<span class="muted">System-managed</span>'
      : '<span class="muted">Sync-managed</span>';
    tr.innerHTML = `
      <td>${s.name}</td>
      <td>${s.email}</td>
      <td style="text-transform:capitalize;">${s.role}</td>
      <td>${statusBadge}</td>
      <td style="white-space:nowrap;">${actions}</td>
    `;
    tbody.appendChild(tr);
  });
}

function startStaffEdit(staffId) {
  const staff = staffCache.find((entry) => entry.id === staffId);
  if (!staff) return;

  editingStaffId = staffId;
  document.getElementById("st-edit-name").value = staff.name || "";
  document.getElementById("st-edit-email").value = staff.email || "";
  document.getElementById("st-edit-role").value = staff.role || "cashier";
  document.getElementById("st-edit-status").value = String(!!staff.is_active);
  document.getElementById("staff-edit-card").style.display = "block";
  document.getElementById("staff-edit-card").scrollIntoView({ behavior: "smooth", block: "start" });
}

function cancelStaffEdit() {
  editingStaffId = null;
  document.getElementById("staff-edit-card").style.display = "none";
}

async function saveStaffEdit() {
  if (!editingStaffId) return;

  const name = document.getElementById("st-edit-name").value.trim();
  const role = document.getElementById("st-edit-role").value;
  const isActive = document.getElementById("st-edit-status").value === "true";

  if (!name) {
    toast("Staff name is required.", "error");
    return;
  }

  try {
    await api(`/staff/${editingStaffId}`, {
      method: "PUT",
      body: JSON.stringify({
        name,
        role,
        is_active: isActive,
      }),
    });
    toast("Staff account updated.", "success");
    cancelStaffEdit();
    await loadStaffPanel();
  } catch (err) {
    toast(err.message, "error");
  }
}

async function createStaff() {
  const name = document.getElementById("st-name").value.trim();
  const email = document.getElementById("st-email").value.trim();
  const password = document.getElementById("st-password").value;
  const role = document.getElementById("st-role").value;

  if (!name || !email) { toast("Enter a name and email.", "error"); return; }
  if (password.length < 6) { toast("Password must be at least 6 characters.", "error"); return; }

  try {
    await api("/staff", {
      method: "POST",
      body: JSON.stringify({ name, email, password, role }),
    });
    toast(`${name}'s account created.`, "success");
    document.getElementById("st-name").value = "";
    document.getElementById("st-email").value = "";
    document.getElementById("st-password").value = "";
    await loadStaffPanel();
  } catch (err) {
    toast(err.message, "error");
  }
}

async function createAdminAccount() {
  const name = document.getElementById("ad-name").value.trim();
  const email = document.getElementById("ad-email").value.trim();
  const password = document.getElementById("ad-password").value;
  if (!name || !email || password.length < 6) { toast("Enter a name, valid email and password of at least 6 characters.", "error"); return; }
  try {
    await api("/staff/admins", { method: "POST", body: JSON.stringify({ name, email, password }) });
    toast("Administrator account created.", "success");
    document.getElementById("ad-name").value = ""; document.getElementById("ad-email").value = ""; document.getElementById("ad-password").value = "";
    await loadStaffPanel();
  } catch (err) { toast(err.message, "error"); }
}

function startPasswordReset(staffId) {
  const s = staffCache.find((x) => x.id === staffId);
  if (!s) return;
  resetPasswordStaffId = staffId;
  document.getElementById("st-reset-name").textContent = s.name;
  document.getElementById("st-reset-password").value = "";
  document.getElementById("st-reset-card").style.display = "block";
  document.getElementById("st-reset-card").scrollIntoView({ behavior: "smooth" });
}

function cancelPasswordReset() {
  resetPasswordStaffId = null;
  document.getElementById("st-reset-card").style.display = "none";
}

async function submitPasswordReset() {
  if (!resetPasswordStaffId) return;
  const newPassword = document.getElementById("st-reset-password").value;
  if (newPassword.length < 6) { toast("Password must be at least 6 characters.", "error"); return; }

  try {
    await api(`/staff/${resetPasswordStaffId}/reset-password`, {
      method: "POST",
      body: JSON.stringify({ new_password: newPassword }),
    });
    toast("Password reset.", "success");
    cancelPasswordReset();
  } catch (err) {
    toast(err.message, "error");
  }
}

async function toggleStaffActive(staffId, makeActive) {
  try {
    await api(`/staff/${staffId}`, {
      method: "PUT",
      body: JSON.stringify({ is_active: makeActive }),
    });
    toast(makeActive ? "Account reactivated." : "Account deactivated.", "success");
    await loadStaffPanel();
  } catch (err) {
    toast(err.message, "error");
  }
}

// ---------- audit log ----------
async function loadAuditLog() {
  try {
    const entries = await api("/audit-log");
    const tbody = document.getElementById("audit-table");
    const emptyEl = document.getElementById("audit-empty");
    tbody.innerHTML = "";

    if (entries.length === 0) {
      emptyEl.style.display = "block";
      return;
    }
    emptyEl.style.display = "none";

    entries.forEach((e) => {
      const tr = document.createElement("tr");
      const details = e.description || "Activity recorded.";
      tr.innerHTML = `
        <td style="white-space:nowrap;font-size:0.78rem;">${new Date(e.created_at).toLocaleString()}</td>
        <td>${e.actor_name || "Unknown"} <span style="color:var(--text-dim);text-transform:capitalize;">(${e.actor_role || "-"})</span></td>
        <td>${e.action.replace(/_/g, " ")}</td>
        <td style="font-size:0.78rem;color:var(--text-secondary);max-width:360px;overflow-wrap:anywhere;">${escapeHtml(details)}</td>
        <td style="font-size:0.78rem;color:var(--warning);max-width:240px;overflow-wrap:anywhere;">${escapeHtml(e.reason || "—")}</td>
      `;
      tbody.appendChild(tr);
    });
  } catch (err) {
    toast(err.message, "error");
  }
}

// ---------- owner system settings / admin quick lock ----------
function getSystemSettings() { return Object.assign({}, DEFAULT_SYSTEM_SETTINGS, systemSettingsCache); }
async function loadSystemSettings() {
  if (!currentStaff || !ADMIN_ROLES.includes(currentStaff.role)) return;
  try {
    const settings = await api("/shop/settings");
    systemSettingsCache = Object.assign({}, DEFAULT_SYSTEM_SETTINGS, {
      timeoutMinutes: Number(settings.timeout_minutes),
      fullLoginHours: Number(settings.full_login_hours),
      pinConfigured: !!settings.pin_configured,
    });
    document.getElementById("setting-timeout").value = String(systemSettingsCache.timeoutMinutes);
    document.getElementById("setting-full-login").value = String(systemSettingsCache.fullLoginHours);
    document.getElementById("setting-pin").value = "";
  } catch (e) { toast(e.message, "error"); }
}
async function saveSystemSettings() {
  if (!currentStaff || !ADMIN_ROLES.includes(currentStaff.role)) {
    toast("Only the shop owner or admin can change system settings.", "error");
    return;
  }
  const timeoutMinutes = Number(document.getElementById("setting-timeout").value);
  const fullLoginHours = Number(document.getElementById("setting-full-login").value);
  const pin = document.getElementById("setting-pin").value.trim();
  if (pin && !/^\d{4}$/.test(pin)) { toast("The quick unlock PIN must be exactly 4 digits.", "error"); return; }
  const btn = document.getElementById("save-settings-btn");
  btn.disabled = true; btn.textContent = "SAVING...";
  try {
    const settings = await api("/shop/settings", { method: "PUT", body: JSON.stringify({ timeout_minutes: timeoutMinutes, full_login_hours: fullLoginHours, pin }) });
    systemSettingsCache = { timeoutMinutes: Number(settings.timeout_minutes), fullLoginHours: Number(settings.full_login_hours), pinConfigured: !!settings.pin_configured };
    document.getElementById("setting-pin").value = "";
    toast("System settings saved successfully.", "success");
  } catch (e) {
    toast(e.message, "error");
  } finally { btn.disabled = false; btn.textContent = "SAVE SETTINGS"; }
}
function touchAdminActivity() {
  if (!currentStaff || !ADMIN_ROLES.includes(currentStaff.role) || adminLocked) return;
  const now = Date.now();
  if (activityTimer) return;
  activityTimer = setTimeout(() => { activityTimer = null; localStorage.setItem("glr_admin_last_active", String(Date.now())); }, 500);
}
function checkAdminSessionSecurity() {
  if (!currentStaff || !ADMIN_ROLES.includes(currentStaff.role)) return;
  const now = Date.now();
  const started = Number(localStorage.getItem("glr_admin_session_started") || now);
  const last = Number(localStorage.getItem("glr_admin_last_active") || started);
  const settings = getSystemSettings();
  if (now - started >= settings.fullLoginHours * 60 * 60 * 1000) { forceAdminLogin(); return; }
  if (now - last >= settings.timeoutMinutes * 60 * 1000) showAdminLock("Your admin session has been inactive for a while. Enter your 4-digit PIN to continue.");
}
function showAdminLock(message) {
  if (adminLocked || !currentStaff || !ADMIN_ROLES.includes(currentStaff.role)) return;
  adminLocked = true;
  document.getElementById("admin-lock-message").textContent = message;
  document.getElementById("admin-lock-pin").value = "";
  document.getElementById("admin-lock-error").textContent = systemSettingsCache.pinConfigured ? "" : "No quick PIN is configured. Use FULL LOGIN.";
  document.getElementById("admin-lock-modal").style.display = "flex";
  setTimeout(() => document.getElementById("admin-lock-pin").focus(), 80);
}
async function unlockAdminSession() {
  const entered = document.getElementById("admin-lock-pin").value.trim();
  const errorEl = document.getElementById("admin-lock-error");
  if (!systemSettingsCache.pinConfigured) { errorEl.textContent = "No quick PIN is configured. Use FULL LOGIN."; return; }
  try {
    await api("/auth/verify-pin", { method: "POST", body: JSON.stringify({ pin: entered }) });
    adminLocked = false;
    localStorage.setItem("glr_admin_last_active", String(Date.now()));
    document.getElementById("admin-lock-modal").style.display = "none";
    toast("Session unlocked.", "success");
  } catch (e) {
    errorEl.textContent = e.message;
    if (/fresh|sign in again|three incorrect|disabled/i.test(e.message)) forceAdminLogin();
  }
}
function forceAdminLogin() {
  adminLocked = false;
  localStorage.removeItem("glr_token"); localStorage.removeItem("glr_staff");
  authToken = null; currentStaff = null;
  document.getElementById("admin-lock-modal").style.display = "none";
  document.getElementById("app").classList.remove("visible");
  document.getElementById("login-overlay").style.display = "flex";
  toast("Please sign in again to continue.", "error");
}
function initializeAdminSecurity() {
  if (!currentStaff || !ADMIN_ROLES.includes(currentStaff.role)) return;
  if (!localStorage.getItem("glr_admin_session_started")) localStorage.setItem("glr_admin_session_started", String(Date.now()));
  localStorage.setItem("glr_admin_last_active", String(Date.now()));
  document.addEventListener("click", touchAdminActivity);
  document.addEventListener("keydown", touchAdminActivity);
  if (adminSecurityTimer) clearInterval(adminSecurityTimer);
  adminSecurityTimer = setInterval(checkAdminSessionSecurity, 15000);
}

// ---------- sync status ----------
let currentHistoryPeriod = "today";

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[ch]));
}

async function viewSale(saleId) {
  try {
    const s = await api(`/sales/${saleId}`);
    const finance = "profit" in s;
    const rows = s.items.map(i => `
      <tr>
        <td><strong>${escapeHtml(i.product_name || `Product #${i.product_id}`)}</strong></td>
        <td>${i.quantity}</td><td>${money(i.unit_price)}</td><td>${money(i.subtotal)}</td>
        ${finance ? `<td>${money(i.profit)}</td>` : ""}
      </tr>`).join("");
    const content = `
      <div class="sale-detail-hero">
        <div><span class="modal-kicker">TRANSACTION</span><div class="sale-detail-invoice">${escapeHtml(s.invoice_number || "PENDING SYNC")}</div><div class="sale-detail-customer">${escapeHtml(s.customer_name || "Walk-in customer")}</div></div>
        <div>${statusTag(s.status)}</div>
      </div>
      <div class="sale-detail-grid">
        <div><span>Date &amp; time</span><strong>${new Date(s.created_at).toLocaleString()}</strong></div>
        <div><span>Payment status</span><strong>${s.status === "completed" ? "Paid in full" : s.status === "voided" ? "Voided" : "Balance outstanding"}</strong></div>
        <div><span>Total sale</span><strong>${money(s.total_amount)}</strong></div>
        <div><span>Collected</span><strong>${money(s.amount_paid)}</strong></div>
        <div><span>Balance</span><strong>${money(s.balance)}</strong></div>
        ${finance ? `<div><span>Profit</span><strong class="positive-value">${money(s.profit)}</strong></div>` : ""}
      </div>
      <div class="sale-items-title">Items sold</div>
      <div class="table-wrap sale-items-table"><table><thead><tr><th>Product</th><th>Qty</th><th>Unit Price</th><th>Subtotal</th>${finance ? "<th>Profit</th>" : ""}</tr></thead><tbody>${rows}</tbody></table></div>
      ${s.void_reason ? `<div class="void-note"><strong>Void reason:</strong> ${escapeHtml(s.void_reason)}</div>` : ""}`;
    document.getElementById("sale-detail-content").innerHTML = content;
    document.getElementById("sale-detail-modal").style.display = "flex";
  } catch (e) { toast(e.message, "error"); }
}

function closeSaleDetail(){document.getElementById("sale-detail-modal").style.display="none";}

function queueHistorySearch() {
  clearTimeout(historySearchTimer);
  historySearchTimer = setTimeout(() => loadHistory(currentHistoryPeriod, document.getElementById("history-search").value.trim()), 250);
}

async function loadHistory(period = "today", search = "") {
  currentHistoryPeriod = period;
  document.querySelectorAll(".history-filter").forEach(btn => btn.classList.toggle("active", btn.getAttribute("onclick") === `loadHistory('${period}')`));
  try {
    const params = new URLSearchParams({ period, limit: "100" });
    if (search) params.set("search", search);
    const sales = await api(`/sales?${params.toString()}`);
    const valid = sales.filter(s => s.status !== "voided");
    document.getElementById("hist-count").textContent = valid.length;
    document.getElementById("hist-revenue").textContent = money(valid.reduce((a,s)=>a+Number(s.total_amount||0),0));
    document.getElementById("hist-paid").textContent = money(valid.reduce((a,s)=>a+Number(s.amount_paid||0),0));
    document.getElementById("hist-balance").textContent = money(valid.reduce((a,s)=>a+Number(s.balance||0),0));
    const finance = valid.some(s => "profit" in s);
    document.getElementById("hist-profit-card").style.display = finance ? "" : "none";
    if (finance) document.getElementById("hist-profit").textContent = money(valid.reduce((a,s)=>a+Number(s.profit||0),0));
    const tbody = document.getElementById("history-table"); tbody.innerHTML = "";
    const empty = document.getElementById("history-empty"); empty.style.display = valid.length ? "none" : "block";
    valid.forEach(s => {
      const tr=document.createElement("tr");
      tr.innerHTML=`<td style="font-family:'DM Mono',monospace;font-size:.78rem;">${escapeHtml(s.invoice_number||"PENDING SYNC")}</td><td>${new Date(s.created_at).toLocaleString()}</td><td>${escapeHtml(s.customer_name||"Walk-in")}</td><td>${escapeHtml(formatSaleProducts(s.items))}</td><td>${getSaleItemCount(s.items)}</td><td>${money(s.total_amount)}</td><td>${money(s.amount_paid)}</td><td>${money(s.balance)}</td>${"profit" in s?`<td>${money(s.profit)}</td>`:""}<td>${statusTag(s.status)}</td><td class="action-cell"><button class="btn btn-secondary btn-sm" onclick="viewSale('${s.id}')">VIEW</button> ${voidActionCell(s)}</td>`;
      tbody.appendChild(tr);
    });
  } catch(err){ toast(err.message,"error"); }
}

async function loadShopBranding(){
  try {
    const shop=await api("/shop");
    currentMode = shop.mode || "local";
    document.getElementById("header-shop-name").textContent=shop.name||"Management System";
    applyRoleVisibility();
  } catch(e){}
}

let syncPollTimer = null;
let syncRequestInFlight = false;

function getDesktopPlatformLabel() {
  const ua = navigator.userAgent || "";
  if (/Windows/i.test(ua)) return "Windows";
  if (/Mac/i.test(ua)) return "macOS";
  if (/Linux/i.test(ua)) return "Linux";
  return "Unknown";
}

async function ensureDeviceRegistration() {
  if (!authToken || !currentStaff || !currentStaff.shop_id) return;

  try {
    const status = await api("/sync/status");
    const deviceId = status && status.device_id ? String(status.device_id) : null;
    if (!deviceId) return;

    localStorage.setItem("glr_device_id", deviceId);

    await loadProvisioningStatus();
  } catch (err) {
    if (err.authExpired || err.networkFailure) return;
    if (/This device is not registered|not registered to an authorized shop/i.test(err.message)) {
      return;
    }
  }
}

function formatSyncTime(value) {
  if (!value) return "never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "unknown";
  return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function pluralize(count, singular, plural = `${singular}s`) {
  return `${count} ${count === 1 ? singular : plural}`;
}

function formatPendingSummary(pendingByTable, fallbackCount) {
  const labels = {
    sales: "sale",
    sale_payments: "payment",
    stock_movements: "stock change",
  };

  const parts = Object.entries(pendingByTable || {})
    .filter(([, count]) => Number(count) > 0)
    .map(([table, count]) => pluralize(Number(count), labels[table] || table.replace(/_/g, " ")));

  return parts.length ? parts.join(" • ") : pluralize(fallbackCount, "change");
}

function setSyncUi({ state, label, detail, pending = 0, title = "", disabled = false }) {
  const wrap = document.getElementById("sync-status");
  const dot = document.getElementById("sync-dot");
  const labelEl = document.getElementById("sync-label");
  const detailEl = document.getElementById("sync-detail");
  const badge = document.getElementById("sync-badge");
  const button = document.getElementById("sync-btn");

  if (!wrap || !dot || !labelEl || !detailEl || !badge || !button) return;

  wrap.classList.remove("sync-good", "sync-busy", "sync-error", "sync-review", "sync-neutral");
  wrap.classList.add(`sync-${state}`);
  labelEl.textContent = label;
  detailEl.textContent = detail;
  wrap.title = title || detail;
  dot.classList.toggle("online", state === "good" || state === "busy");

  if (pending > 0) {
    badge.style.display = "inline-block";
    badge.textContent = `${pending} waiting`;
  } else {
    badge.style.display = "none";
  }

  button.disabled = !!disabled;
  button.textContent = syncRequestInFlight ? "SYNCING..." : "SYNC NOW";
}

function startSyncStatusPolling() {
  pollSyncStatus();
  clearInterval(syncPollTimer);
  syncPollTimer = setInterval(() => {
    if (document.visibilityState === "visible") {
      pollSyncStatus();
    }
  }, 10000);
}

async function triggerSync(options) {
  const silent = !!(options && options.silent);
  if (syncRequestInFlight) return;

  syncRequestInFlight = true;
  setSyncUi({
    state: "busy",
    label: "Syncing",
    detail: "Sending queued changes to the central server…",
    disabled: true,
  });

  try {
    await api("/sync/trigger", { method: "POST" });
    if (!silent) syncToast("Synchronization started. The status will update when the local worker finishes.", "info");
    await pollSyncStatus();
    setTimeout(pollSyncStatus, 1000);
  } catch (e) {
    if (!e.authExpired && !e.networkFailure && !silent) syncToast(e.message, "error");
    await pollSyncStatus();
  } finally {
    syncRequestInFlight = false;
    await pollSyncStatus();
  }
}

let lastSyncSnapshot = { key: null, pending: 0 };

async function pollSyncStatus() {
  if (!authToken) return;
  try {
    const data = await api("/sync/status");

    const pending = Number(data.pending_count || 0);
    const pendingSummary = formatPendingSummary(data.pending_by_table, pending);
    const needsReview = Number(data.needs_review_count || 0);
    const lastPush = formatSyncTime(data.last_sync_at);
    const lastPull = formatSyncTime(data.last_pull_at);
    const pushError = data.last_sync_error || "";
    const pullError = data.last_pull_error || "";
    const state = data.sync_state || "synced";
    const pushProblem = !!pushError;
    const pullProblem = !!pullError;

    if (data.mode !== "local") {
      setSyncUi({
        state: "good",
        label: "Central mode",
        detail: "Connected to central server",
        title: "Central deployment is handling synchronization directly.",
      });
      lastSyncSnapshot = { key: "central", pending: 0 };
      return;
    }

    if (data.provisioning_state === "NOT_ENROLLED") {
      setSyncUi({
        state: "neutral",
        label: "Not enrolled",
        detail: "Device authorization is required",
        title: "This desktop is not yet authorized for synchronization.",
      });
      lastSyncSnapshot = { key: "not_enrolled", pending: 0 };
      return;
    }

    if (data.provisioning_state === "ENROLLED / PROVISIONING") {
      setSyncUi({
        state: "busy",
        label: "Setting up sync",
        detail: "Downloading business data from central server…",
        title: "The device is completing its initial synchronization.",
      });
      lastSyncSnapshot = { key: "provisioning", pending: 0 };
      return;
    }

    let key = state;
    let label = "Synced";
    let detail = `All changes sent • Last upload ${lastPush}`;
    let uiState = "good";

    if (needsReview > 0) {
      key = "review";
      label = "Needs attention";
      detail = `${pluralize(needsReview, "change")} could not be synchronized and needs review`;
      uiState = "review";
    } else if (pending > 0 && pushProblem) {
      key = "retrying";
      label = "Retrying upload";
      detail = `${pendingSummary} waiting • retrying upload automatically`;
      uiState = "error";
    } else if (pending > 0) {
      key = "pending";
      label = syncRequestInFlight ? "Syncing" : "Queued for sync";
      detail = pullProblem
        ? `${pendingSummary} waiting • central refresh delayed`
        : `${pendingSummary} queued • automatic upload active`;
      uiState = syncRequestInFlight ? "busy" : "busy";
    } else if (pushProblem || pullProblem) {
      key = "error";
      label = pullProblem && !pushProblem ? "Central refresh delayed" : "Central sync issue";
      detail = pushProblem
        ? "No queued changes, but the last upload reported an error"
        : "All local changes are sent • the last central refresh reported an error";
      uiState = "error";
    } else {
      key = "synced";
      label = "Synced";
      detail = lastPush === "never" && lastPull === "never"
        ? "No synchronization has completed yet"
        : `All changes sent • Upload ${lastPush} • Refresh ${lastPull}`;
      uiState = "good";
    }

    const errorDetail = [
      pushProblem ? `Upload error: ${pushError}` : "",
      pullProblem ? `Refresh error: ${pullError}` : "",
    ].filter(Boolean).join("\n");

    setSyncUi({
      state: uiState,
      label,
      detail,
      pending,
      title: errorDetail || `${detail}. Click SYNC NOW to send queued changes immediately.`,
      disabled: syncRequestInFlight,
    });

    const prev = lastSyncSnapshot;
    if (prev.key !== key) {
      if (key === "error" || (key === "pending" && centralProblem)) {
        syncToast(
          pending > 0
            ? `${pending} ${pending === 1 ? "change is" : "changes are"} waiting for central synchronization.`
            : "Central synchronization reported an error.",
          "warning"
        );
      } else if (prev.key === "error" && (key === "synced" || key === "pending")) {
        syncToast(
          key === "synced"
            ? "Central synchronization restored. All queued changes are clear."
            : "Central synchronization restored. Queued changes are being sent.",
          "success"
        );
      } else if ((prev.key === "pending" || prev.key === "error") && key === "synced") {
        syncToast("Synchronization complete. All queued changes reached the central server.", "success");
      } else if (key === "review" && prev.key !== "review") {
        syncToast(`${needsReview} synchronization ${needsReview === 1 ? "item needs" : "items need"} owner review.`, "warning");
      }
    }
    lastSyncSnapshot = { key, pending };

  } catch (err) {
    setSyncUi({
      state: "error",
      label: "POS server unavailable",
      detail: "The local sync service cannot be reached",
      title: "The desktop's local Flask service is unavailable.",
      disabled: syncRequestInFlight,
    });
  }
}

async function loadLoginBranding() {
  try {
    const res = await fetch(API_BASE + "/shop/public");
    if (!res.ok) return;
    const shop = await res.json();
    const title = document.getElementById("login-shop-title");
    if (title && shop.name) title.textContent = String(shop.name).toUpperCase();
    if (shop.name) document.title = shop.name;
  } catch (_) {}
}

// ---------- desktop app updater (Tauri only; independent of POS sync) ----------
let updatePromptDismissedThisSession = false;
let updateInstallInProgress = false;
let pendingUpdateInfo = null;

function tauriInvoke(cmd, args) {
  const invoke = window.__TAURI__ && window.__TAURI__.core && window.__TAURI__.core.invoke;
  if (typeof invoke !== "function") return Promise.resolve(null);
  return invoke(cmd, args || {});
}

function sleepMs(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function probeLocalBackendHealth() {
  const res = await fetch(API_BASE + "/health", { method: "GET" });
  return !!res && res.ok;
}

/**
 * Wait until the local sidecar answers /api/health on 127.0.0.1.
 * If Tauri reports a spawn failure, surface that message immediately.
 */
async function waitForLocalBackend(options = {}) {
  if (localBackendReady) return true;

  const timeoutMs = options.timeoutMs != null ? options.timeoutMs : LOCAL_BACKEND_READY_TIMEOUT_MS;
  const intervalMs = options.intervalMs != null ? options.intervalMs : LOCAL_BACKEND_POLL_MS;
  const startedAt = Date.now();

  try {
    const status = await tauriInvoke("glr_local_backend_status");
    if (status && status.spawnError) {
      const error = new Error(String(status.spawnError));
      error.networkFailure = true;
      error.sidecarFailed = true;
      throw error;
    }
  } catch (err) {
    if (err && err.sidecarFailed) throw err;
    // Non-Tauri browsers / missing command: fall through to health polling.
  }

  while (Date.now() - startedAt < timeoutMs) {
    try {
      if (await probeLocalBackendHealth()) {
        localBackendReady = true;
        return true;
      }
    } catch (_) {
      // Sidecar still starting (PyInstaller extract / Flask boot).
    }
    await sleepMs(intervalMs);
  }

  const error = new Error(
    "The local POS server did not become ready. Confirm Good Luck Rahman is fully started, then try again."
  );
  error.networkFailure = true;
  throw error;
}

function showUpdateToast(info) {
  const el = document.getElementById("update-toast");
  const msg = document.getElementById("update-toast-message");
  const nowBtn = document.getElementById("update-now-btn");
  const laterBtn = document.getElementById("update-later-btn");
  if (!el || !msg || !info) return;
  pendingUpdateInfo = info;
  msg.textContent = `New version ${info.version} is available.`;
  if (nowBtn) { nowBtn.disabled = false; nowBtn.textContent = "Update now"; }
  if (laterBtn) laterBtn.disabled = false;
  el.classList.add("show");
  el.style.display = "grid";
}

function hideUpdateToast() {
  const el = document.getElementById("update-toast");
  if (!el) return;
  el.classList.remove("show");
  el.style.display = "none";
}

function dismissAppUpdate() {
  updatePromptDismissedThisSession = true;
  hideUpdateToast();
}

async function installAppUpdate() {
  if (updateInstallInProgress) return;
  updateInstallInProgress = true;
  const nowBtn = document.getElementById("update-now-btn");
  const laterBtn = document.getElementById("update-later-btn");
  if (nowBtn) { nowBtn.disabled = true; nowBtn.textContent = "Updating..."; }
  if (laterBtn) laterBtn.disabled = true;
  try {
    await tauriInvoke("glr_install_update");
    // Process should relaunch; if it returns, keep UI honest.
  } catch (err) {
    updateInstallInProgress = false;
    if (nowBtn) { nowBtn.disabled = false; nowBtn.textContent = "Update now"; }
    if (laterBtn) laterBtn.disabled = false;
    toast(err && err.message ? err.message : "Update failed. You can try again later.", "error");
  }
}

async function checkForAppUpdate() {
  if (updatePromptDismissedThisSession || updateInstallInProgress) return;
  try {
    const info = await tauriInvoke("glr_check_update");
    if (info && info.version) showUpdateToast(info);
  } catch (_) {
    // Network/GitHub unavailable must never interrupt POS.
  }
}

function scheduleAppUpdateCheck() {
  // One delayed background check after UI is ready. No recurring timer —
  // avoids sync/session races and notification floods.
  setTimeout(() => { checkForAppUpdate(); }, 8000);
}

// ---------- boot ----------
(async function boot() {
  const syncDot = document.getElementById("sync-dot");
  const syncLabel = document.getElementById("sync-label");
  if (syncLabel) syncLabel.textContent = "Starting local POS server...";

  try {
    await waitForLocalBackend();
    if (syncLabel && syncLabel.textContent === "Starting local POS server...") {
      syncLabel.textContent = "Ready";
    }
  } catch (err) {
    if (syncDot) syncDot.classList.remove("online");
    if (syncLabel) syncLabel.textContent = "POS server unavailable";
    toast(
      (err && err.message) || "The local POS server is unavailable.",
      "error"
    );
  }

  loadLoginBranding();
  loadProvisioningStatus();
  scheduleAppUpdateCheck();
  window.addEventListener("online", async () => {
    if (authToken && currentStaff) {
      try {
        await ensureDeviceRegistration();
      } catch (_) {}
    }
  });
  document.getElementById("login-password").addEventListener("keydown", (e) => {
    if (e.key === "Enter") doLogin();
  });
  document.getElementById("s-customer").addEventListener("blur", (e) => normalizeInputName(e.target));
  document.getElementById("s-customer").addEventListener("input", (e) => { e.target.value = e.target.value.replace(/[^a-zA-Z\s\-'\.]/g, ""); });
  document.getElementById("admin-lock-pin").addEventListener("keydown", (e) => {
    if (e.key === "Enter") unlockAdminSession();
  });
  setInterval(() => {
    if (currentStaff && !adminLocked && document.getElementById("panel-sales")?.classList.contains("active")) {
      loadProducts(true).then(loadSalesPanel);
    }
  }, 30000);
})();
