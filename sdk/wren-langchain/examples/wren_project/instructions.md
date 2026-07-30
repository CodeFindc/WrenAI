# Wren Sample Project

## Business rules

- All amounts are in USD.
- `orders.total_with_tax` = `orders.total * 1.1` (10% sales tax).
- `customers.full_name` = `customers.first_name || ' ' || customers.last_name`.

## Canonical tables

- Use `orders` for order analytics; each order belongs to exactly one
  `customer` (MANY_TO_ONE via `orders.customer_id = customers.customer_id`).
- Use `customers` for customer analytics; traverse `orders` from a customer
  with `customers.orders` is NOT modeled — start order-side instead.

## Formatting

- Currency is USD; display with 2 decimals.
- `customer_id` / `order_id` are integers, never sensitive.
