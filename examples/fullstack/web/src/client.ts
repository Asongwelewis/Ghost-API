// The frontend agent's API client, with the mistakes Ghost catches.
import axios from "axios";

const api = axios.create({ baseURL: "/api" });

export async function loadOrder(id: string) {
  return api.get(`/orders/${id}`); // fine
}

export async function placeOrder() {
  return fetch("/api/orders", { method: "POST", body: "{}" }); // fine
}

export async function loadOrderTypo(id: string) {
  return fetch(`/api/order/${id}`); // unknown-endpoint: the route is /api/orders/{id}
}

export async function updateOrder(id: string) {
  return api.put(`/orders/${id}`); // wrong-method: no PUT route
}

export async function retry() {
  return axios.fetchh("/api/orders"); // unknown-symbol: axios has no fetchh
}
