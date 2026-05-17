import * as React from 'react'
import { useTranslation } from 'react-i18next'
import { Zap } from 'lucide-react'
import { SkillAvatar } from '@/components/ui/skill-avatar'
import { EntityPanel } from '@/components/ui/entity-panel'
import { EntityListEmptyScreen } from '@/components/ui/entity-list-empty'
import { skillSelection } from '@/hooks/useEntitySelection'
import { SkillMenu } from './SkillMenu'
import { SendResourceToWorkspaceDialog } from './SendResourceToWorkspaceDialog'
import { EditPopover, getEditConfig } from '@/components/ui/EditPopover'
import { useActiveWorkspace, useAppShellContext } from '@/context/AppShellContext'
import type { LoadedSkill } from '../../../shared/types'

const KNOWN_CATEGORY_ORDER = [
  'builtin',
  'file-operations',
  'finance',
  'visualization',
  'writing',
  'research',
  'observability',
  'uncategorized',
]

function fallbackCategoryLabel(category: string) {
  return category
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ')
}

export interface SkillsListPanelProps {
  skills: LoadedSkill[]
  onDeleteSkill: (skillSlug: string) => void
  onSkillClick: (skill: LoadedSkill) => void
  selectedSkillSlug?: string | null
  workspaceId?: string
  workspaceRootPath?: string
  className?: string
}

export function SkillsListPanel({
  skills,
  onDeleteSkill,
  onSkillClick,
  selectedSkillSlug,
  workspaceId,
  workspaceRootPath,
  className,
}: SkillsListPanelProps) {
  const { t } = useTranslation()
  const activeWorkspace = useActiveWorkspace()
  const canRevealLocally = !activeWorkspace?.remoteServer
  const { workspaces, activeWorkspaceId } = useAppShellContext()
  const hasOtherWorkspaces = workspaces.length > 1

  const categoryLabels = React.useMemo<Record<string, string>>(() => ({
    builtin: t('skillsCategory.builtin'),
    'file-operations': t('skillsCategory.fileOperations'),
    finance: t('skillsCategory.finance'),
    visualization: t('skillsCategory.visualization'),
    writing: t('skillsCategory.writing'),
    research: t('skillsCategory.research'),
    observability: t('skillsCategory.observability'),
    uncategorized: t('skillsList.uncategorized'),
  }), [t])

  const getCategoryKey = React.useCallback((skill: LoadedSkill) => (
    skill.metadata.category?.trim() || 'uncategorized'
  ), [])

  const getCategoryLabel = React.useCallback((category: string) => (
    categoryLabels[category] || fallbackCategoryLabel(category)
  ), [categoryLabels])

  const skillGroups = React.useMemo(() => {
    const grouped = new Map<string, LoadedSkill[]>()
    for (const skill of skills) {
      const category = getCategoryKey(skill)
      const group = grouped.get(category)
      if (group) group.push(skill)
      else grouped.set(category, [skill])
    }

    return Array.from(grouped.entries())
      .sort(([a], [b]) => {
        const aIndex = KNOWN_CATEGORY_ORDER.indexOf(a)
        const bIndex = KNOWN_CATEGORY_ORDER.indexOf(b)
        if (aIndex !== -1 || bIndex !== -1) {
          return (aIndex === -1 ? Number.MAX_SAFE_INTEGER : aIndex)
            - (bIndex === -1 ? Number.MAX_SAFE_INTEGER : bIndex)
        }
        return getCategoryLabel(a).localeCompare(getCategoryLabel(b))
      })
      .map(([category, items]) => ({
        key: category,
        label: getCategoryLabel(category),
        items,
      }))
  }, [getCategoryKey, getCategoryLabel, skills])

  // Send to Workspace dialog state
  const [sendDialogOpen, setSendDialogOpen] = React.useState(false)
  const [sendResourceSlug, setSendResourceSlug] = React.useState<string | null>(null)
  const [sendResourceLabel, setSendResourceLabel] = React.useState('')

  return (
    <>
    <EntityPanel<LoadedSkill>
      items={skills}
      groups={skillGroups}
      getId={(s) => s.slug}
      selection={skillSelection}
      selectedId={selectedSkillSlug}
      onItemClick={onSkillClick}
      className={className}
      containerProps={{ 'data-list-role': 'skills' }}
      emptyState={
        <EntityListEmptyScreen
          icon={<Zap />}
          title={t('skillsList.noSkillsConfigured')}
          description={t('skillsList.emptyDescription')}
          docKey="skills"
        >
          {workspaceRootPath && (
            <EditPopover
              align="center"
              trigger={
                <button className="inline-flex items-center h-7 px-3 text-xs font-medium rounded-[8px] bg-background shadow-minimal hover:bg-foreground/[0.03] transition-colors">
                  {t('skillsList.addSkill')}
                </button>
              }
              {...getEditConfig('add-skill', workspaceRootPath)}
            />
          )}
        </EntityListEmptyScreen>
      }
      mapItem={(skill) => ({
        icon: <SkillAvatar skill={skill} size="sm" workspaceId={workspaceId} />,
        title: skill.metadata.name,
        badges: (
          <span className="flex items-center gap-1.5 min-w-0">
            {skill.source === 'project' && (
              <span className="shrink-0 text-[10px] px-1.5 py-0.5 rounded-full bg-foreground/5 text-muted-foreground">
                {t('skillsList.projectBadge')}
              </span>
            )}
            <span className="shrink-0 text-[10px] px-1.5 py-0.5 rounded-full bg-foreground/5 text-muted-foreground">
              {getCategoryLabel(getCategoryKey(skill))}
            </span>
            <span className="truncate">{skill.metadata.description}</span>
          </span>
        ),
        menu: (
          <SkillMenu
            skillSlug={skill.slug}
            skillName={skill.metadata.name}
            onOpenInNewWindow={() => window.electronAPI.openUrl(`craftagents://skills/skill/${skill.slug}?window=focused`)}
            onShowInFinder={() => {
              if (canRevealLocally) {
                void window.electronAPI.showInFolder(`${skill.path}/SKILL.md`)
              }
            }}
            canShowInFinder={canRevealLocally}
            onDelete={skill.source === 'workspace' ? () => onDeleteSkill(skill.slug) : undefined}
            canDelete={skill.source === 'workspace'}
            deleteLabel={skill.source === 'workspace' ? t('skillsList.deleteSkill') : t('skillsList.managedByProject')}
            onSendToWorkspace={hasOtherWorkspaces && skill.source === 'workspace' ? () => {
              setSendResourceSlug(skill.slug)
              setSendResourceLabel(skill.metadata.name)
              setSendDialogOpen(true)
            } : undefined}
          />
        ),
      })}
    />

    {/* Send to Workspace dialog */}
    {sendResourceSlug && (
      <SendResourceToWorkspaceDialog
        open={sendDialogOpen}
        onOpenChange={setSendDialogOpen}
        resourceType="skill"
        resourceIds={[sendResourceSlug]}
        resourceLabel={sendResourceLabel}
        workspaces={workspaces}
        activeWorkspaceId={activeWorkspaceId}
      />
    )}
    </>
  )
}
