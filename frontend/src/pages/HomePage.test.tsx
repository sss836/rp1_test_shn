// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { HomePage } from './HomePage';
import { getDashboardHome } from '../api';
import type { DashboardHome } from '../types';

vi.mock('../api', () => ({ getDashboardHome: vi.fn() }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it.each(['WHOLE_MACHINE', 'MODULE'] as const)('renders model and navigable test cases for %s with zero samples', async (kind) => {
  vi.stubGlobal('matchMedia', () => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  const part = kind === 'MODULE' ? 'SLEG' : 'SYS';
  const zero = { total_duration_seconds: 0, effective_exposure_seconds: 0, excluded_seconds: 0, pending_seconds: 0, active_elapsed_seconds: 0 };
  const home = { freshness: { status: 'NO_DATA', as_of_at: '2026-10-08T00:00:00Z', data_cutoff_at: null }, data_sources: { sources: [], healthy_count: 0, warning_count: 0 }, anomaly_count: 0, anomalies: [], groups: [{ asset_kind: kind, objects: [{ id: part, code: part, name: '参考模型', target_part_name: '参考模型', target_part_code: part, asset_kind: kind, status: 'NO_DATA', part_asset_count: 0, active_execution_count: 0, abnormal_asset_count: 0, part_durations: zero, test_cases: [1, 2].map(n => ({ id: String(n), code: `QA-${part}-${n}`, name: `目录用例${n}`, domain: 'RELIABILITY', current_elapsed_seconds: 0, cumulative_duration_seconds: 0, effective_exposure_seconds: 0, excluded_seconds: 0, pending_seconds: 0, execution_count: 0, scope_asset_count: 0, target_duration_seconds: null, target_progress_percent: null })) }] }] } as unknown as DashboardHome;
  if (kind === 'MODULE') {
    const scope = home.groups[0].objects[0];
    home.groups[0].objects.push({ ...scope, id: 'SARM', code: 'SARM', target_part_code: 'SARM', target_part_name: '单臂' });
    home.groups.push({ asset_kind: 'WHOLE_MACHINE', objects: [{ ...scope, id: 'SYS', code: 'SYS', target_part_code: 'SYS', target_part_name: '整机', asset_kind: 'WHOLE_MACHINE' }] });
  }
  vi.mocked(getDashboardHome).mockResolvedValue(home);
  const navigate = vi.fn();
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(<QueryClientProvider client={cache}><HomePage assetKind={kind} navigate={navigate} /></QueryClientProvider>);
  const image = await screen.findByAltText('参考模型外观');
  expect(image.getAttribute('src')).toBe(kind === 'MODULE' ? '/model-posters/single-leg-product-white.webp' : '/assets/humanoid-robot.webp');
  expect(screen.getByText('0 台样品 / 2 项用例')).toBeTruthy();
  expect(screen.queryByText(/尚无可访问/)).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '下一个 Test Case' }));
  expect(document.querySelector('.case-flip-viewport')?.getAttribute('data-active-index')).toBe('1');
  if (kind === 'MODULE') {
    fireEvent.click(screen.getByRole('button', { name: /单臂\s*SARM · 0 台/ }));
    expect(document.querySelector('.object-identity > span')?.textContent).toContain('02 / 02');
    view.rerender(<QueryClientProvider client={cache}><HomePage assetKind="WHOLE_MACHINE" navigate={navigate} /></QueryClientProvider>);
    expect(screen.getByAltText('整机外观')).toBeTruthy();
    expect(document.querySelector('.object-identity > span')?.textContent).toContain('01 / 01');
    expect(document.querySelector('.asset-index > span')?.textContent).toContain('/ 01');
  }
  vi.unstubAllGlobals();
});
