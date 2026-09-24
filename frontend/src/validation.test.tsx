import { describe, expect, it } from 'vitest';
import { BacklogSchema, GatewaySchema, ProjectSchema, SprintSchema, TaskSchema } from './validation';

const item = { title: ' Build API ', description: '', acceptance_criteria: '',
  story_points: '3', priority: '2' };

describe('work item validation', () => {
  it('trims title and converts number inputs', () => {
    const parsed = TaskSchema.parse(item);
    expect(parsed.title).toBe('Build API');
    expect(parsed.story_points).toBe(3);
    expect(parsed.priority).toBe(2);
  });

  it('rejects empty titles and out-of-range numbers', () => {
    expect(BacklogSchema.safeParse({ ...item, title: '  ' }).success).toBe(false);
    expect(TaskSchema.safeParse({ ...item, story_points: '0' }).success).toBe(false);
    expect(TaskSchema.safeParse({ ...item, priority: '6' }).success).toBe(false);
  });

  it('validates project, sprint and gateway creation', () => {
    expect(ProjectSchema.safeParse({ name: ' ', goal: '', description: '',
      technology_stack: '', repository_url: '', workspace_path: '' }).success).toBe(false);
    expect(SprintSchema.safeParse({ name: 'S1', goal: '', capacity: '0' }).success).toBe(false);
    expect(GatewaySchema.safeParse({ name: 'GW', base_url: 'javascript:alert(1)' }).success).toBe(false);
    expect(GatewaySchema.safeParse({ name: 'GW', base_url: 'https://example.test/v1' }).success).toBe(true);
  });
});
