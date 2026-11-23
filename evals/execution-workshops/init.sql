-- Loaded only by the optional eval-db Compose profile; never by the Sidewit API.
CREATE ROLE sidewit_eval LOGIN PASSWORD 'sidewit_eval';
ALTER ROLE sidewit_eval SET default_transaction_read_only = on;
ALTER ROLE sidewit_eval SET statement_timeout = '5s';

CREATE TABLE members (
  id BIGINT PRIMARY KEY, name TEXT NOT NULL, city TEXT NOT NULL,
  status TEXT NOT NULL, joined_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE workshops (
  id BIGINT PRIMARY KEY, title TEXT NOT NULL, category TEXT NOT NULL,
  capacity INTEGER NOT NULL, starts_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE bookings (
  id BIGINT PRIMARY KEY, member_id BIGINT NOT NULL REFERENCES members(id),
  workshop_id BIGINT NOT NULL REFERENCES workshops(id), state TEXT NOT NULL,
  price NUMERIC NOT NULL, booked_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE checkins (
  id BIGINT PRIMARY KEY, booking_id BIGINT NOT NULL UNIQUE REFERENCES bookings(id),
  checked_in_at TIMESTAMPTZ NOT NULL
);

INSERT INTO members VALUES
  (1, '민지', '서울', 'ACTIVE', '2026-09-01T09:00:00+09:00'),
  (2, '준호', '부산', 'ACTIVE', '2026-09-15T09:00:00+09:00'),
  (3, '서연', '서울', 'INACTIVE', '2026-10-02T09:00:00+09:00'),
  (4, '도윤', '대구', 'ACTIVE', '2026-08-20T09:00:00+09:00'),
  (5, '하린', '부산', 'ACTIVE', '2026-10-05T09:00:00+09:00'),
  (6, '지우', '서울', 'ACTIVE', '2026-10-10T09:00:00+09:00'),
  (7, '수현', '광주', 'INACTIVE', '2026-07-01T09:00:00+09:00'),
  (8, '유진', '부산', 'ACTIVE', '2026-09-30T09:00:00+09:00'),
  (9, '현우', '대전', 'ACTIVE', '2026-10-14T09:00:00+09:00'),
  (10, '나영', '서울', 'ACTIVE', '2026-08-31T09:00:00+09:00'),
  (11, '태오', '제주', 'ACTIVE', '2026-09-28T09:00:00+09:00'),
  (12, '다은', '부산', 'INACTIVE', '2026-10-01T09:00:00+09:00');

INSERT INTO workshops VALUES
  (1, 'AI 입문', 'coding', 20, '2026-10-20T10:00:00+09:00'),
  (2, '도예', 'art', 12, '2026-10-22T10:00:00+09:00'),
  (3, '재즈', 'music', 30, '2026-11-05T10:00:00+09:00'),
  (4, '파이썬 심화', 'coding', 15, '2026-09-25T10:00:00+09:00'),
  (5, '수채화', 'art', 25, '2026-10-05T10:00:00+09:00'),
  (6, '기타', 'music', 18, '2026-09-15T10:00:00+09:00'),
  (7, '데이터 분석', 'coding', 35, '2026-11-10T10:00:00+09:00'),
  (8, '사진', 'art', 10, '2026-08-20T10:00:00+09:00');

INSERT INTO bookings VALUES
  (1, 1, 1, 'PAID', 20000, '2026-09-01T10:00:00+09:00'),
  (2, 2, 1, 'PAID', 20000, '2026-09-10T10:00:00+09:00'),
  (3, 3, 1, 'CANCELLED', 20000, '2026-09-11T10:00:00+09:00'),
  (4, 4, 1, 'PENDING', 20000, '2026-10-01T10:00:00+09:00'),
  (5, 5, 2, 'PAID', 30000, '2026-09-20T10:00:00+09:00'),
  (6, 6, 2, 'PAID', 30000, '2026-10-03T10:00:00+09:00'),
  (7, 7, 2, 'CANCELLED', 30000, '2026-10-04T10:00:00+09:00'),
  (8, 8, 3, 'PAID', 25000, '2026-10-05T10:00:00+09:00'),
  (9, 9, 3, 'PAID', 25000, '2026-10-08T10:00:00+09:00'),
  (10, 10, 3, 'PENDING', 25000, '2026-10-09T10:00:00+09:00'),
  (11, 1, 4, 'PAID', 40000, '2026-09-12T10:00:00+09:00'),
  (12, 2, 4, 'CANCELLED', 40000, '2026-09-13T10:00:00+09:00'),
  (13, 4, 4, 'PAID', 40000, '2026-09-14T10:00:00+09:00'),
  (14, 5, 5, 'PAID', 15000, '2026-10-02T10:00:00+09:00'),
  (15, 6, 5, 'PAID', 15000, '2026-10-10T10:00:00+09:00'),
  (16, 8, 5, 'PENDING', 15000, '2026-10-11T10:00:00+09:00'),
  (17, 10, 6, 'PAID', 18000, '2026-08-20T10:00:00+09:00'),
  (18, 12, 6, 'PAID', 18000, '2026-08-25T10:00:00+09:00'),
  (19, 3, 6, 'PAID', 18000, '2026-09-01T10:00:00+09:00'),
  (20, 7, 7, 'PAID', 50000, '2026-10-12T10:00:00+09:00'),
  (21, 9, 7, 'PENDING', 50000, '2026-10-13T10:00:00+09:00'),
  (22, 1, 7, 'PAID', 50000, '2026-10-14T10:00:00+09:00'),
  (23, 2, 7, 'CANCELLED', 50000, '2026-10-14T11:00:00+09:00'),
  (24, 5, 1, 'PAID', 20000, '2026-10-14T10:00:00+09:00'),
  (25, 6, 1, 'PAID', 20000, '2026-10-14T11:00:00+09:00'),
  (26, 8, 1, 'PAID', 20000, '2026-10-14T12:00:00+09:00');

INSERT INTO checkins VALUES
  (1, 1, '2026-10-20T10:00:00+09:00'),
  (2, 2, '2026-10-20T10:01:00+09:00'),
  (3, 5, '2026-10-22T10:00:00+09:00'),
  (4, 6, '2026-10-22T10:01:00+09:00'),
  (5, 8, '2026-11-05T10:00:00+09:00'),
  (6, 11, '2026-09-25T10:00:00+09:00'),
  (7, 13, '2026-09-25T10:01:00+09:00'),
  (8, 14, '2026-10-05T10:00:00+09:00'),
  (9, 17, '2026-09-15T10:00:00+09:00'),
  (10, 20, '2026-11-10T10:00:00+09:00'),
  (11, 24, '2026-10-20T10:02:00+09:00');

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE sidewit_eval TO sidewit_eval;
GRANT USAGE ON SCHEMA public TO sidewit_eval;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO sidewit_eval;
