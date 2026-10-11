const categoryWeights = {
  highScale: 2,
  dataIntegrity: 2,
  hardSecurity: 3,
  technicalDebt: 4,
  manualArchitecture: 2,
  noiseFilter: -10,
};

const categories = {
  // Category 1: High-Scale Architecture & Concurrency Control (+2 Points)
  highScale: [
    /\b(asynchronous callbacks)\b/i, /\b(asynchronous processing)\b/i, /\b(background jobs)\b/i,
    /\b(background tasks)\b/i, /\b(cluster architecture)\b/i, /\b(cold start mitigation)\b/i,
    /\b(concurrency control)\b/i, /\b(concurrency primitives)\b/i, /\b(concurrent requests)\b/i,
    /\b(concurrent test suites)\b/i, /\b(connection pool exhaustion)\b/i, /\b(connection pooling)\b/i,
    /\b(distributed architecture)\b/i, /\b(distributed computing)\b/i, /\b(distributed execution)\b/i,
    /\b(distributed state)\b/i, /\b(distributed systems)\b/i, /\b(distributed task queue)\b/i,
    /\b(durable execution)\b/i, /\b(durable workflows)\b/i, /\b(edge computing)\b/i,
    /\b(edge optimization)\b/i, /\b(event loop blocking)\b/i, /\b(event streams)\b/i,
    /\b(event-driven architecture)\b/i, /\b(event-driven pipelines)\b/i, /\b(execution speed)\b/i,
    /\b(fault-tolerant systems)\b/i, /\b(gpu scheduling)\b/i, /\b(high availability)\b/i,
    /\b(high throughput)\b/i, /\b(high-throughput systems)\b/i, /\b(horizontally scalable)\b/i,
    /\b(in-memory processing)\b/i, /\b(latency optimization)\b/i, /\b(lock contention)\b/i,
    /\b(log ingestion)\b/i, /\b(low latency)\b/i, /\b(low-latency apis)\b/i, /\b(memory leaks)\b/i,
    /\b(memory management)\b/i, /\b(message broker)\b/i, /\b(microservice architectures)\b/i,
    /\b(multi-threading)\b/i, /\b(non-blocking i\/o)\b/i, /\b(queue management)\b/i,
    /\b(race conditions)\b/i, /\b(rate limiting)\b/i, /\b(real-time inference)\b/i,
    /\b(real-time interaction)\b/i, /\b(serverless functions)\b/i, /\b(serverless execution)\b/i,
    /\b(state machines)\b/i, /\b(stateful inference)\b/i, /\b(stateless functions)\b/i,
    /\b(thread pool)\b/i, /\b(worker threads)\b/i
  ],

  // Category 2: Data Integrity & Pipeline Engineering (+2 Points)
  dataIntegrity: [
    /\b(append-only event log)\b/i, /\b(audit trail)\b/i, /\b(auditable data models)\b/i,
    /\b(background jobs system)\b/i, /\b(complex data engines)\b/i, /\b(data infrastructure)\b/i,
    /\b(data ingestion)\b/i, /\b(data pipeline concepts)\b/i, /\b(data synchronization)\b/i,
    /\b(database connections)\b/i, /\b(distributed etl)\b/i, /\b(elt workflows)\b/i,
    /\b(entity resolution)\b/i, /\b(etl \/ elt)\b/i, /\b(etl workflows)\b/i,
    /\b(event-driven log processing)\b/i, /\b(high-throughput data pipelines)\b/i, /\b(highly reliable)\b/i,
    /\b(idempotent csv imports)\b/i, /\b(lockstep synchronization)\b/i, /\b(log ingestion pipelines)\b/i,
    /\b(messy data)\b/i, /\b(multi-platform synchronization)\b/i, /\b(observability and logging)\b/i,
    /\b(on-chain data indexing)\b/i, /\b(persistent connections)\b/i, /\b(pipeline engineering)\b/i,
    /\b(real-world datasets)\b/i, /\b(relational data modeling)\b/i, /\b(resilient sync layer)\b/i,
    /\b(schema migrations)\b/i, /\b(state updates)\b/i, /\b(structured extraction)\b/i,
    /\b(sync layer)\b/i, /\b(transaction processing)\b/i, /\b(transaction safety)\b/i,
    /\b(transactional guarantees)\b/i, /\b(transactional safety)\b/i, /\b(unstructured sources)\b/i,
    /\b(workflow automation)\b/i, /\b(workflow integrations)\b/i
  ],

  // Category 3: Hard Security & Integration Controls (+3 Points)
  hardSecurity: [
    /\b(access control)\b/i, /\b(asynchronous updates)\b/i, /\b(authorization capabilities)\b/i,
    /\b(dedicated api routes)\b/i, /\b(dynamic claims engines)\b/i, /\b(edge optimization)\b/i,
    /\b(fault-tolerant)\b/i, /\b(fine grained access)\b/i, /\b(hipaa-aware)\b/i,
    /\b(idempotency)\b/i, /\b(idempotent endpoints)\b/i, /\b(idempotent replay patterns)\b/i,
    /\b(idempotent upsert)\b/i, /\b(identity systems)\b/i, /\b(jwt\/rbac authentication)\b/i,
    /\b(key rotation)\b/i, /\b(microservice communication)\b/i, /\b(multi-tenant architecture)\b/i,
    /\b(multi-tenant by)\b/i, /\b(multi-tenant identity)\b/i, /\b(multi-tenant isolation)\b/i,
    /\b(multi-tenant saas)\b/i, /\b(multi-tenant security)\b/i, /\b(oauth 2\.0)\b/i,
    /\b(oidc flows)\b/i, /\b(partial failures)\b/i, /\b(pragmatic security)\b/i,
    /\b(retries and fallbacks)\b/i, /\b(role-based access)\b/i, /\b(role-based systems)\b/i,
    /\b(row-level security)\b/i, /\b(secure integrations)\b/i, /\b(security invoker)\b/i,
    /\b(signature validation)\b/i, /\b(sso integration)\b/i, /\b(strong tenant isolation)\b/i,
    /\b(tenant isolation)\b/i, /\b(webhook architecture)\b/i, /\b(webhook ordering)\b/i,
    /\b(webhook sigs)\b/i, /\b(webhook signature validation)\b/i, /\b(workload identity)\b/i
  ],

  // Category 4: Technical Debt & Urgency Signals (+4 Points)
  technicalDebt: [
    /\b(aggressively refactor)\b/i, /\b(architecture decisions)\b/i, /\b(bottlenecks)\b/i,
    /\b(builder mentality)\b/i, /\b(code stagnation)\b/i, /\b(completely overhaul)\b/i,
    /\b(complex workflows)\b/i, /\b(core infrastructure)\b/i, /\b(duct-tape fix)\b/i,
    /\b(end-to-end delivery)\b/i, /\b(failing test)\b/i, /\b(founding engineer autonomy)\b/i,
    /\b(founding urgency)\b/i, /\b(go 0 to 1)\b/i, /\b(legacy monoliths)\b/i,
    /\b(major scalability win)\b/i, /\b(major technical decisions)\b/i, /\b(messy data)\b/i,
    /\b(messy ownership)\b/i, /\b(messy real-world)\b/i, /\b(migration)\b/i,
    /\b(modernize legacy)\b/i, /\b(operational edge cases)\b/i, /\b(overhaul)\b/i,
    /\b(production-quality code)\b/i, /\b(production-ready)\b/i, /\b(rebuild the prototype)\b/i,
    /\b(refactor aggressively)\b/i, /\b(rewrite that buys)\b/i, /\b(rewrite the prototype)\b/i,
    /\b(rock-solid)\b/i, /\b(scaling bottlenecks)\b/i, /\b(ship imperfect)\b/i,
    /\b(spaghetti code)\b/i, /\b(systems foundation)\b/i, /\b(technical debt)\b/i,
    /\b(trail of technical debt)\b/i, /\b(turn messy)\b/i, /\b(unglamorous work)\b/i,
    /\b(unstable prototype)\b/i, /\b(zero to one)\b/i
  ],

  // Manual Additions: Deep Architecture (+2 Points)
  manualArchitecture: [
    /\b(concurrency)\b/i, /\b(race conditions)\b/i, /\b(thread-safe)\b/i,
    /\b(grpc)\b/i, /\b(protocol buffers)\b/i, /\b(websockets)\b/i,
    /\b(idempotency)\b/i, /\b(message queues)\b/i, /\b(rabbitmq)\b/i,
    /\b(kafka)\b/i, /\b(database indexing)\b/i, /\b(query optimization)\b/i,
    /\b(acid)\b/i, /\b(multi-tenant)\b/i, /\b(rbac)\b/i, /\b(role-based access control)\b/i
  ],

  // Category 5: Noise Filter & Manual Exclusions (-10 Points / Instant Veto)
  noiseFilter: [
    /\b(bubble\.io)\b/i, /\b(build a landing page)\b/i, /\b(cms integration)\b/i,
    /\b(css fixes)\b/i, /\b(elementor)\b/i, /\b(figma to html)\b/i,
    /\b(figma to react)\b/i, /\b(framer x)\b/i, /\b(framer)\b/i,
    /\b(freelance web designer)\b/i, /\b(front-end assembly)\b/i, /\b(html css javascript)\b/i,
    /\b(html\/css tweaks)\b/i, /\b(landing page optimization)\b/i, /\b(landing page)\b/i,
    /\b(low-code)\b/i, /\b(marketing site)\b/i, /\b(no-code app)\b/i,
    /\b(no-code builder)\b/i, /\b(no-code tools)\b/i, /\b(no-code)\b/i,
    /\b(pixel perfect)\b/i, /\b(shopify theme)\b/i, /\b(simple layout change)\b/i,
    /\b(simple wrapper)\b/i, /\b(squarespace)\b/i, /\b(static assets only)\b/i,
    /\b(static site)\b/i, /\b(tailwind components)\b/i, /\b(theme customization)\b/i,
    /\b(thin ai wrapper)\b/i, /\b(thin wrapper)\b/i, /\b(ui adjustments)\b/i,
    /\b(ui tweaks)\b/i, /\b(webflow builder)\b/i, /\b(webflow developer)\b/i,
    /\b(webflow)\b/i, /\b(wix)\b/i, /\b(wordpress backend)\b/i,
    /\b(wordpress theme)\b/i, /\b(wordpress)\b/i,
    // Manual "Avoid These" words
    /\b(prompt tuning)\b/i, /\b(ai integration)\b/i, /\b(chatbot ui)\b/i,
    /\b(rest api consumption)\b/i, /\b(third-party api)\b/i, /\b(zapier)\b/i,
    /\b(make\.com)\b/i, /\b(no-code workflow)\b/i, /\b(figma to webflow)\b/i
  ]
};

export function evaluateLeadIntent({ title = '', description = '', company = '' } = {}) {
  const text = `${title} ${description} ${company}`.toLowerCase();
  const matches = {};
  let score = 0;

  Object.entries(categories).forEach(([category, patterns]) => {
    const matched = patterns.some((pattern) => pattern.test(text));
    matches[category] = matched;
    if (matched) {
      score += categoryWeights[category] || 1;
    }
  });

  const isExcluded = matches.noiseFilter;
  return { score, matches, isExcluded };
}

export { categoryWeights, categories };
