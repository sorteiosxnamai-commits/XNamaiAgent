# XNaMai Club backend contract

Source: `sorteiosxnamai-commits/xnamai-club-backend` at `a54df58`.
The route files and `src/server.ts` are authoritative; its README and Postman
collection contain older registration and payment examples.

| Route | Access | Actual response or input |
| --- | --- | --- |
| `POST /api/auth/register` | public | `name`, `email`, `password` (8+), `city`, `state`, valid `document`; optional `phone`, `companyName`; returns JWT and user |
| `POST /api/auth/login` | public | email/password; returns JWT and user |
| `GET /api/auth/me` | customer JWT | user profile |
| `GET /api/plans` | public | array of active Plan entities, ordered by `sortOrder` |
| `POST /api/subscriptions/checkout` | customer JWT | `planId`, optional `paymentMethodType`; returns Stripe checkout URL |
| `GET /api/subscriptions/confirm?sessionId=...` | customer JWT | confirmed subscription and next billing date |
| `GET /api/subscriptions/me` | customer JWT | latest subscription or 404 |
| `GET /api/me/dashboard` | customer JWT | subscription, payment method, invoices |
| `GET /api/atendimento/members` | JWT + role `ADMIN` or `SUPPORT` | PII-rich member lists for the Club's own support screen; the agent must not call it |
| `POST /api/atendimento/members/:id/cashback-use` | JWT + role `ADMIN` or `SUPPORT` | marks the launch cashback as used |

The agent's `ClubProvider` consumes only public `GET /api/plans`, validates the
actual array and Plan fields, and verbalizes a current price only after a
successful response. It does not collect password, hold a JWT, call Stripe,
create a subscription, or assert membership from a WhatsApp message.
The signup and checkout steps occur on the Club site under customer login.
The Club dialogue stores only whether the person says they have an account or
subscription; those claims are not verified membership status. It asks about
the account first, then the subscription, and directs the customer to the
appropriate authenticated step on the Club site.
Hardening applied in the Club backend: `/atendimento` requires a JWT with
role `ADMIN` or `SUPPORT` (the Club frontend routes `SUPPORT` to that screen);
routes exist only under `/api` (the duplicate root mount was removed — the
frontend, Postman, Render health check and Stripe webhook all use `/api`);
production refuses to start without `JWT_SECRET`.

If the agent ever needs to verify membership, it needs a dedicated endpoint
that answers for ONE identified customer — never the support list.
