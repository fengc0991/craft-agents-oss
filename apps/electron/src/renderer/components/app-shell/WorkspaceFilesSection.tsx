/**
 * WorkspaceFilesSection - workspace-root file browser in the primary sidebar.
 *
 * Shows user-visible files from the active workspace root while filtering out
 * Craft's own configuration/session/source folders on the server.
 */

import * as React from 'react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ChevronRight, FolderOpen } from 'lucide-react'
import { cn } from '@/lib/utils'
import * as storage from '@/lib/local-storage'
import { FileTree, type FileTreeEntry } from '@/components/files/FileTree'
import { getFileManagerName } from '@/lib/platform'
import type { AppShellContextType } from '@/context/AppShellContext'

interface WorkspaceFilesSectionProps {
  workspaceId: string | null
  workspaceRootPath?: string
  expanded: boolean
  onToggle: () => void
  onOpenFile: AppShellContextType['onOpenFile']
  className?: string
}

export function WorkspaceFilesSection({
  workspaceId,
  workspaceRootPath,
  expanded,
  onToggle,
  onOpenFile,
  className,
}: WorkspaceFilesSectionProps) {
  const { t } = useTranslation()
  const [files, setFiles] = useState<FileTreeEntry[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(new Set())
  const [selectedPath, setSelectedPath] = useState<string | null>(null)
  const mountedRef = useRef(true)
  const fileManagerName = getFileManagerName()

  const storageSuffix = workspaceId ? `workspace:${workspaceId}` : undefined

  useEffect(() => {
    if (!workspaceId || !storageSuffix) {
      setExpandedPaths(new Set())
      setSelectedPath(null)
      return
    }

    const saved = storage.get<string[]>(storage.KEYS.workspaceFilesExpandedFolders, [], storageSuffix)
    setExpandedPaths(new Set(saved))
    setSelectedPath(null)
  }, [workspaceId, storageSuffix])

  const saveExpandedPaths = useCallback((paths: Set<string>) => {
    if (!storageSuffix) return
    storage.set(storage.KEYS.workspaceFilesExpandedFolders, Array.from(paths), storageSuffix)
  }, [storageSuffix])

  const loadFiles = useCallback(async () => {
    if (!workspaceId) {
      setFiles([])
      return
    }

    setIsLoading(true)
    try {
      const workspaceFiles = await window.electronAPI.getWorkspaceFiles(workspaceId)
      if (mountedRef.current) {
        setFiles(workspaceFiles)
      }
    } catch (error) {
      console.error('Failed to load workspace files:', error)
      if (mountedRef.current) {
        setFiles([])
      }
    } finally {
      if (mountedRef.current) {
        setIsLoading(false)
      }
    }
  }, [workspaceId])

  useEffect(() => {
    mountedRef.current = true
    void loadFiles()

    if (workspaceId) {
      void window.electronAPI.watchWorkspaceFiles(workspaceId)

      const unsubscribeFiles = window.electronAPI.onWorkspaceFilesChanged((changedWorkspaceId) => {
        if (changedWorkspaceId === workspaceId && mountedRef.current) {
          void loadFiles()
        }
      })

      const unsubscribeReconnect = window.electronAPI.onReconnected(() => {
        if (!mountedRef.current) return
        void window.electronAPI.watchWorkspaceFiles(workspaceId).finally(() => {
          if (mountedRef.current) void loadFiles()
        })
      })

      return () => {
        mountedRef.current = false
        unsubscribeFiles()
        unsubscribeReconnect()
        void window.electronAPI.unwatchWorkspaceFiles()
      }
    }

    return () => {
      mountedRef.current = false
    }
  }, [workspaceId, loadFiles])

  const totalFileCount = useMemo(() => {
    let count = 0
    const visit = (items: FileTreeEntry[]) => {
      for (const item of items) {
        if (item.type === 'file') count += 1
        if (item.children) visit(item.children)
      }
    }
    visit(files)
    return count
  }, [files])

  const handleToggleExpand = useCallback((path: string) => {
    setExpandedPaths((prev) => {
      const next = new Set(prev)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      saveExpandedPaths(next)
      return next
    })
  }, [saveExpandedPaths])

  const handleRevealInFileManager = useCallback((path: string) => {
    void window.electronAPI.showInFolder(path)
  }, [])

  const handleFileClick = useCallback((file: FileTreeEntry) => {
    setSelectedPath(file.path)
    if (file.type === 'directory') {
      // eslint-disable-next-line craft-links/no-direct-file-open -- directories can't be previewed in-app
      void window.electronAPI.openFile(file.path)
    } else {
      onOpenFile(file.path)
    }
  }, [onOpenFile])

  const handleFileDoubleClick = useCallback((file: FileTreeEntry) => {
    handleFileClick(file)
  }, [handleFileClick])

  if (!workspaceId) return null

  return (
    <div className={cn("select-none", className)}>
      <div className="px-2">
        <button
          type="button"
          onClick={onToggle}
          className={cn(
            "group flex w-full items-center gap-2 rounded-[6px] py-[5px] px-2 text-[13px] select-none outline-none text-left",
            "focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-ring hover:bg-sidebar-hover"
          )}
          title={workspaceRootPath}
        >
          <span className="relative h-3.5 w-3.5 shrink-0 flex items-center justify-center">
            <FolderOpen className="h-3.5 w-3.5 text-muted-foreground group-hover:opacity-0 transition-opacity duration-150" />
            <ChevronRight
              className={cn(
                "absolute inset-0 h-3.5 w-3.5 text-muted-foreground opacity-0 group-hover:opacity-100 transition-all duration-200",
                expanded && "rotate-90"
              )}
            />
          </span>
          <span className="flex-1 min-w-0 truncate">{t('settings.workspace.title')}</span>
          {totalFileCount > 0 && (
            <span className="text-xs text-foreground/30 opacity-0 group-hover:opacity-100 transition-opacity">
              {totalFileCount}
            </span>
          )}
        </button>
      </div>

      {expanded && (
        <div className="mt-1 mb-2">
          {files.length === 0 ? (
            <div className="px-4 py-1 text-xs text-muted-foreground">
              {isLoading ? t('chat.sessionFilesLoading') : t('workspace.filesEmpty', 'No workspace files')}
            </div>
          ) : (
            <FileTree
              files={files}
              expandedPaths={expandedPaths}
              selectedPath={selectedPath}
              onToggleExpand={handleToggleExpand}
              onFileClick={handleFileClick}
              onFileDoubleClick={handleFileDoubleClick}
              onRevealInFileManager={handleRevealInFileManager}
            />
          )}
          {workspaceRootPath && (
            <button
              type="button"
              onClick={() => window.electronAPI.showInFolder(workspaceRootPath)}
              className="ml-4 mt-1 text-xs text-foreground/40 hover:text-foreground/75 hover:underline underline-offset-2"
            >
              {t("chat.viewInFileManager", { fileManager: fileManagerName })}
            </button>
          )}
        </div>
      )}
    </div>
  )
}
