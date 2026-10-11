-- Metadata input for Sidewit. Sidewit parses this DDL; it never executes it.
CREATE TABLE customers (
    id BIGINT PRIMARY KEY,
    name TEXT,
    region TEXT NOT NULL
);

CREATE TABLE orders (
    id BIGINT PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES customers(id),
    amount NUMERIC(12, 2),
    status TEXT NOT NULL,
    ordered_at TIMESTAMPTZ NOT NULL,
    paid_at TIMESTAMPTZ
);

CREATE TABLE shipments (
    id BIGINT PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES orders(id),
    carrier TEXT,
    created_at TIMESTAMPTZ,
    processed_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);
