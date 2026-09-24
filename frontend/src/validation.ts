import { z } from 'zod';

const workItem = z.object({
  title: z.string().trim().min(1, 'Title is required').max(200, 'Title is too long'),
  description: z.string().max(2000, 'Description is too long'),
  acceptance_criteria: z.string().max(2000, 'Acceptance criteria are too long'),
  story_points: z.coerce.number<number>().int().min(1, 'Story points must be at least 1')
    .max(13, 'Story points cannot exceed 13'),
  priority: z.coerce.number<number>().int().min(1, 'Priority must be between 1 and 5')
    .max(5, 'Priority must be between 1 and 5'),
});

export const TaskSchema = workItem.passthrough();
export const BacklogSchema = workItem.passthrough();

export const ProjectSchema = z.object({
  name: z.string().trim().min(1, 'Project name is required').max(200),
  goal: z.string().max(2000),
  description: z.string().max(5000),
  technology_stack: z.string().max(500),
  repository_url: z.string().max(1000),
  workspace_path: z.string().max(1000),
}).passthrough();

export const SprintSchema = z.object({
  name: z.string().trim().min(1, 'Sprint name is required').max(200),
  goal: z.string().max(2000),
  capacity: z.coerce.number<number>().int().min(1, 'Capacity must be at least 1')
    .max(1000, 'Capacity cannot exceed 1000'),
}).passthrough();

export const GatewaySchema = z.object({
  name: z.string().trim().min(1, 'Gateway name is required').max(200),
  base_url: z.string().trim().url('Enter a valid gateway URL')
    .refine((url) => /^https?:\/\//i.test(url), 'Gateway URL must use HTTP or HTTPS'),
}).passthrough();
