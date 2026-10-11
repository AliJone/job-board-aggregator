import test from 'node:test';
import assert from 'node:assert/strict';
import { evaluateLeadIntent } from '../js/lead_intent_scoring.js';

test('awards +2 for manual architecture phrases', () => {
  const result = evaluateLeadIntent({
    title: 'Senior Platform Engineer',
    description: 'Designing a multi-tenant system with gRPC and role-based access control.'
  });

  assert.equal(result.score, 2);
  assert.equal(result.matches.manualArchitecture, true);
});

test('vetoes jobs matching the noise filter bucket', () => {
  const result = evaluateLeadIntent({
    title: 'Webflow Developer',
    description: 'Build a landing page and simple wrapper for a no-code app.'
  });

  assert.equal(result.isExcluded, true);
  assert.equal(result.score, -10);
});
