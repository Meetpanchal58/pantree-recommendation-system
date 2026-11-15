// Pantree storefront: shared browser helpers (cart, wishlist, toasts, menus).
"use strict";

// Product photo failed to load (offline/blocked): draw a clean placeholder instead of a broken image.
function imgFallback(el) {
  const name = el.dataset.name || "Pantree";
  const cat = el.dataset.cat || "";
  const label = name.length > 18 ? name.slice(0, 18) : name;
  const svg = "<svg xmlns='http://www.w3.org/2000/svg' width='400' height='400'>" +
    "<rect width='100%' height='100%' fill='#e7f1ec'/>" +
    "<text x='50%' y='47%' fill='#0f3d35' font-family='Georgia' font-size='30' font-weight='bold' text-anchor='middle'>" + label + "</text>" +
    "<text x='50%' y='58%' fill='#66746f' font-family='Arial' font-size='17' text-anchor='middle'>" + cat + "</text></svg>";
  el.onerror = null;
  el.src = "data:image/svg+xml;utf8," + encodeURIComponent(svg);
}

function toast(message) {
  const t = document.getElementById("toast");
  if (!t) return;
  t.textContent = message;
  t.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove("show"), 1900);
}

async function postForm(url, fields) {
  const body = new FormData();
  Object.entries(fields || {}).forEach(([key, value]) => body.append(key, value));
  const response = await fetch(url, { method: "POST", body, credentials: "same-origin" });
  if (response.status === 401) {
    location.href = "/login";
    return null;
  }
  try {
    return await response.json();
  } catch (error) {
    return { ok: false, message: "Something went wrong. Please try again." };
  }
}

function setCartCount(count) {
  document.querySelectorAll("[data-cart-count]").forEach((badge) => { badge.textContent = count; });
}

function quantityControlHtml(pid, quantity) {
  return '<div class="card-quantity"><small>In your cart</small><div class="quantity-control">' +
    '<button type="button" onclick="updateCartQuantity(\'' + pid + '\',-1)" aria-label="Decrease quantity">−</button>' +
    '<output data-quantity>' + quantity + '</output>' +
    '<button type="button" onclick="updateCartQuantity(\'' + pid + '\',1)" aria-label="Increase quantity">+</button></div></div>';
}

async function toggleWishlist(pid, button) {
  const d = await postForm("/wishlist/toggle", { product_id: pid });
  if (!d) return;
  if (!d.ok) { toast(d.message || "Wishlist update failed"); return; }
  const buttons = [...document.querySelectorAll(".wishlist-button[data-product-id]")].filter((item) => item.dataset.productId === pid);
  if (!buttons.includes(button)) buttons.push(button);
  buttons.forEach((item) => {
    item.classList.toggle("is-wishlisted", d.wishlisted);
    item.setAttribute("aria-pressed", String(d.wishlisted));
    item.setAttribute("aria-label", d.wishlisted ? "Remove from wishlist" : "Add to wishlist");
    item.textContent = d.wishlisted ? "♥" : "♡";
  });
  toast(d.wishlisted ? "Added to wishlist" : "Removed from wishlist");
}

async function addToCart(pid, qty = 1) {
  const d = await postForm("/cart/add", { product_id: pid, qty });
  if (!d) return false;
  if (!d.ok) { toast(d.message || "Could not add item"); return false; }
  setCartCount(d.cart_count);
  const recommendationCards = [...document.querySelectorAll(".recommendation-card")].filter((card) => card.dataset.productId === pid);
  if (recommendationCards.length && typeof applyRecommendationCartState === "function") {
    recommendationCards.forEach((card) => applyRecommendationCartState(card, d.quantity));
    toast("Added to cart");
    return true;
  }
  const card = document.querySelector('[data-product-id="' + pid + '"]');
  const button = card && card.querySelector(".add-cart-button");
  if (button) button.outerHTML = quantityControlHtml(pid, d.quantity);
  toast("Added to cart");
  return true;
}

async function buyNow(pid) {
  if (await addToCart(pid)) location.href = "/cart";
}

async function updateCartQuantity(pid, delta) {
  const d = await postForm("/cart/update", { product_id: pid, delta });
  if (!d) return;
  if (!d.ok) { toast(d.message || "Could not update quantity"); return; }
  setCartCount(d.cart_count);
  const recommendationCards = [...document.querySelectorAll(".recommendation-card")].filter((card) => card.dataset.productId === pid);
  if (recommendationCards.length && typeof applyRecommendationCartState === "function") {
    recommendationCards.forEach((card) => applyRecommendationCartState(card, d.quantity));
    return;
  }
  const item = document.querySelector('[data-product-id="' + pid + '"]');
  if (item && d.quantity === 0) { location.reload(); return; }
  if (item) item.querySelectorAll("[data-quantity]").forEach((el) => { el.value = d.quantity; el.textContent = d.quantity; });
  const lineTotal = item && item.querySelector("[data-line-total]");
  const rupees = (amount) => "₹" + Math.round(amount).toLocaleString("en-IN");
  if (lineTotal) lineTotal.textContent = rupees(Number(lineTotal.dataset.unitPrice || item.dataset.unitPrice || 0) * d.quantity);
  const grandTotal = document.querySelectorAll("[data-grand-total]");
  if (grandTotal.length) {
    let total = 0;
    document.querySelectorAll("[data-product-id][data-unit-price]").forEach((line) => {
      total += Number(line.dataset.unitPrice || 0) * Number(line.querySelector("[data-quantity]")?.textContent || 0);
    });
    grandTotal.forEach((el) => { el.textContent = rupees(total); });
  }
}

async function refreshInv() {
  const d = await postForm("/inventory/refresh", {});
  if (!d) return;
  toast(d.message || "Restocked");
  setTimeout(() => location.reload(), 800);
}

// Close the account menu when clicking elsewhere or pressing Escape.
document.addEventListener("click", (event) => {
  document.querySelectorAll("details.account-menu[open]").forEach((menu) => {
    if (!menu.contains(event.target)) menu.removeAttribute("open");
  });
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") document.querySelectorAll("details.account-menu[open]").forEach((menu) => menu.removeAttribute("open"));
});
