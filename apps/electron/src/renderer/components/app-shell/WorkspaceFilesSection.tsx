/**
 * WorkspaceFilesSection - workspace-root file browser in the primary sidebar.
 *
 * Shows user-visible files from the active workspace root while filtering out
 * Craft's own configuration/session/source folders on the server.
 */

import * as React from 'react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ChevronRight, Copy, FolderOpen, FolderPlus } from 'lucide-react'
import { toast } from 'sonner'
import { cn } from '@/lib/utils'
import * as storage from '@/lib/local-storage'
import { FileTree, type FileTreeEntry } from '@/components/files/FileTree'
import { getFileManagerName } from '@/lib/platform'
import type { AppShellContextType } from '@/context/AppShellContext'
import { Button } from '@/components/ui/button'
import {
  ContextMenu,
  ContextMenuTrigger,
  StyledContextMenuContent,
  StyledContextMenuItem,
  StyledContextMenuSeparator,
} from '@/components/ui/styled-context-menu'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { RenameDialog } from '@/components/ui/rename-dialog'

interface WorkspaceFilesSectionProps {
  workspaceId: string | null
  workspaceRootPath?: string
  expanded: boolean
  onToggle: () => void
  onOpenFile: AppShellContextType['onOpenFile']
  className?: string
}

type FolderDialogState =
  | { type: 'create'; parentPath: string }
  | { type: 'rename'; folder: FileTreeEntry }

function getErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function isSameOrChildPath(path: string, parentPath: string): boolean {
  return path === parentPath || path.startsWith(`${parentPath}/`) || path.startsWith(`${parentPath}\\`)
}

function remapPathPrefix(path: string, oldPrefix: string, newPrefix: string): string {
  return isSameOrChildPath(path, oldPrefix)
    ? `${newPrefix}${path.slice(oldPrefix.length)}`
    : path
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
  const [folderDialog, setFolderDialog] = useState<FolderDialogState | null>(null)
  const [folderName, setFolderName] = useState('')
  const [deleteTarget, setDeleteTarget] = useState<FileTreeEntry | null>(null)
  const [isMutating, setIsMutating] = useState(false)
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

  const handleCopyPath = useCallback(async (path: string) => {
    try {
      await navigator.clipboard.writeText(path)
      toast.success(t('toast.pathCopied'))
    } catch {
      toast.error(t('toast.copyFailed'))
    }
  }, [t])

  const handleOpenCreateFolderDialog = useCallback((parentPath: string) => {
    setFolderDialog({ type: 'create', parentPath })
    setFolderName('')
  }, [])

  const handleOpenRenameFolderDialog = useCallback((folder: FileTreeEntry) => {
    if (folder.type !== 'directory') return
    setFolderDialog({ type: 'rename', folder })
    setFolderName(folder.name)
  }, [])

  const handleSubmitFolderDialog = useCallback(async () => {
    if (!workspaceId || !folderDialog) return

    const name = folderName.trim()
    if (!name) return

    setIsMutating(true)
    try {
      if (folderDialog.type === 'create') {
        const createdPath = await window.electronAPI.createWorkspaceFolder(workspaceId, folderDialog.parentPath, name)
        setExpandedPaths((prev) => {
          const next = new Set(prev)
          next.add(folderDialog.parentPath)
          saveExpandedPaths(next)
          return next
        })
        setSelectedPath(createdPath)
      } else {
        const oldPath = folderDialog.folder.path
        const newPath = await window.electronAPI.renameWorkspacePath(workspaceId, oldPath, name)
        setExpandedPaths((prev) => {
          const next = new Set<string>()
          for (const path of prev) {
            next.add(remapPathPrefix(path, oldPath, newPath))
          }
          saveExpandedPaths(next)
          return next
        })
        setSelectedPath((current) => current ? remapPathPrefix(current, oldPath, newPath) : current)
      }

      setFolderDialog(null)
      setFolderName('')
      await loadFiles()
      toast.success(t('common.done'))
    } catch (error) {
      toast.error(t('common.failed'), { description: getErrorMessage(error) })
    } finally {
      if (mountedRef.current) {
        setIsMutating(false)
      }
    }
  }, [folderDialog, folderName, loadFiles, saveExpandedPaths, t, workspaceId])

  const handleDeleteFolder = useCallback((folder: FileTreeEntry) => {
    if (folder.type !== 'directory') return
    setDeleteTarget(folder)
  }, [])

  const handleConfirmDeleteFolder = useCallback(async () => {
    if (!workspaceId || !deleteTarget) return

    const targetPath = deleteTarget.path
    setIsMutating(true)
    try {
      await window.electronAPI.deleteWorkspacePath(workspaceId, targetPath)
      setExpandedPaths((prev) => {
        const next = new Set(Array.from(prev).filter(path => !isSameOrChildPath(path, targetPath)))
        saveExpandedPaths(next)
        return next
      })
      setSelectedPath((current) => current && isSameOrChildPath(current, targetPath) ? null : current)
      setDeleteTarget(null)
      await loadFiles()
      toast.success(t('common.done'))
    } catch (error) {
      toast.error(t('common.failed'), { description: getErrorMessage(error) })
    } finally {
      if (mountedRef.current) {
        setIsMutating(false)
      }
    }
  }, [deleteTarget, loadFiles, saveExpandedPaths, t, workspaceId])

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

  const workspaceHeaderButton = (
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
  )

  return (
    <div className={cn("select-none", className)}>
      <div className="px-2">
        {workspaceRootPath ? (
          <ContextMenu>
            <ContextMenuTrigger asChild>
              {workspaceHeaderButton}
            </ContextMenuTrigger>
            <StyledContextMenuContent minWidth="min-w-44">
              <StyledContextMenuItem onSelect={() => handleOpenCreateFolderDialog(workspaceRootPath)}>
                <FolderPlus className="h-3.5 w-3.5" />
                {t("common.newFolder")}
              </StyledContextMenuItem>
              <StyledContextMenuSeparator />
              <StyledContextMenuItem onSelect={() => handleCopyPath(workspaceRootPath)}>
                <Copy className="h-3.5 w-3.5" />
                {t("common.copyPath")}
              </StyledContextMenuItem>
              <StyledContextMenuItem onSelect={() => window.electronAPI.showInFolder(workspaceRootPath)}>
                <FolderOpen className="h-3.5 w-3.5" />
                {t("chat.showInFileManager", { fileManager: fileManagerName })}
              </StyledContextMenuItem>
            </StyledContextMenuContent>
          </ContextMenu>
        ) : workspaceHeaderButton}
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
              onCreateFolder={(directory) => handleOpenCreateFolderDialog(directory.path)}
              onRenameFolder={handleOpenRenameFolderDialog}
              onDeleteFolder={handleDeleteFolder}
              onCopyPath={handleCopyPath}
              withRootGuides
            />
          )}
        </div>
      )}

      <RenameDialog
        open={folderDialog !== null}
        onOpenChange={(open) => {
          if (!open && !isMutating) {
            setFolderDialog(null)
            setFolderName('')
          }
        }}
        title={folderDialog?.type === 'rename'
          ? t("common.renameFolder")
          : t("common.newFolder")}
        value={folderName}
        onValueChange={setFolderName}
        onSubmit={handleSubmitFolderDialog}
        placeholder={t("common.enterName")}
        submitLabel={folderDialog?.type === 'create' ? t("common.create") : t("common.save")}
      />

      <Dialog
        open={deleteTarget !== null}
        onOpenChange={(open) => {
          if (!open && !isMutating) setDeleteTarget(null)
        }}
      >
        <DialogContent className="sm:max-w-[420px]">
          <DialogHeader>
            <DialogTitle>{t("common.deleteFolder")}</DialogTitle>
            <DialogDescription>
              {t("dialog.deleteFolderConfirmation", { name: deleteTarget?.name ?? '' })}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteTarget(null)} disabled={isMutating}>
              {t("common.cancel")}
            </Button>
            <Button variant="destructive" onClick={handleConfirmDeleteFolder} disabled={isMutating}>
              {t("common.delete")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
