"""One registry for runtime and release-history writing fingerprints."""
WRITING_FILES = frozenset({
    'story_prompt.py', 'story_generation.py', 'story_scoring.py', 'config/story.py',
    'applications/zhihu_story/prompts.py', 'workflows/workflow_generation.py',
    'core/reference_sample.py', 'core/story_editor.py', 'core/story_length.py',
    'core/writing_sources.py',
})

# Absent new modules must not alter fingerprints of earlier releases.
OPTIONAL_WRITING_FILES = frozenset({
    'core/reference_sample.py', 'core/story_editor.py', 'core/story_length.py',
    'core/writing_sources.py',
})
