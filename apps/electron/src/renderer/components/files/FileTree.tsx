/**
 * FileTree - reusable VS Code-style file tree.
 *
 * Used by workspace/session file browsers. It intentionally delegates the
 * actual file action to callers so they can decide between in-app preview,
 * external open, or navigation.
 */

import * as React from 'react'
import { memo, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { AnimatePresence, motion, type Variants } from 'motion/react'
import {
  File,
  Folder,
  FolderOpen,
  FileText,
  Image,
  FileCode,
  ChevronRight,
  Download,
  ExternalLink,
  FolderPlus,
  Pencil,
  Copy,
  Trash2,
} from 'lucide-react'
import {
  ContextMenu,
  ContextMenuTrigger,
  StyledContextMenuContent,
  StyledContextMenuItem,
  StyledContextMenuSeparator,
} from '@/components/ui/styled-context-menu'
import type { SessionFile } from '../../../shared/types'
import { cn } from '@/lib/utils'
import { getFileManagerName } from '@/lib/platform'

export type FileTreeEntry = SessionFile

const containerVariants: Variants = {
  hidden: { opacity: 0 },
  visible: {
    opacity: 1,
    transition: {
      staggerChildren: 0.025,
      delayChildren: 0.01,
    },
  },
  exit: {
    opacity: 0,
    transition: {
      staggerChildren: 0.015,
      staggerDirection: -1,
    },
  },
}

const itemVariants: Variants = {
  hidden: { opacity: 0, x: -8 },
  visible: {
    opacity: 1,
    x: 0,
    transition: { duration: 0.15, ease: 'easeOut' },
  },
  exit: {
    opacity: 0,
    x: -8,
    transition: { duration: 0.1, ease: 'easeIn' },
  },
}

function formatFileSize(bytes?: number): string {
  if (bytes === undefined) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export function collectDirectoryPaths(entries: FileTreeEntry[]): string[] {
  const directories: string[] = []
  const visit = (items: FileTreeEntry[]) => {
    for (const item of items) {
      if (item.type === 'directory') {
        directories.push(item.path)
        if (item.children && item.children.length > 0) {
          visit(item.children)
        }
      }
    }
  }
  visit(entries)
  return directories
}

function getFileIcon(file: FileTreeEntry, isExpanded?: boolean) {
  const iconClass = "h-3.5 w-3.5 text-muted-foreground"

  if (file.type === 'directory') {
    return isExpanded
      ? <FolderOpen className={iconClass} />
      : <Folder className={iconClass} />
  }

  const ext = file.name.split('.').pop()?.toLowerCase()

  if (ext === 'md' || ext === 'markdown') {
    return <FileText className={iconClass} />
  }

  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'ico'].includes(ext || '')) {
    return <Image className={iconClass} />
  }

  if (['ts', 'tsx', 'js', 'jsx', 'json', 'yaml', 'yml', 'py', 'rb', 'go', 'rs'].includes(ext || '')) {
    return <FileCode className={iconClass} />
  }

  return <File className={iconClass} />
}

const PREVIEWABLE_EXTENSIONS = new Set([
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'tiff', 'tif', 'ico', 'heic', 'heif',
  'pdf', 'svg', 'psd', 'ai',
])

const WEB_PREVIEWABLE_EXTENSIONS = new Set([
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico',
])

const isWebMode = window.electronAPI.getRuntimeEnvironment() === 'web'

function getThumbnailUrl(filePath: string): string {
  return `thumbnail://thumb/${encodeURIComponent(filePath)}`
}

const FileThumbnail = memo(function FileThumbnail({ file }: { file: FileTreeEntry }) {
  const [loaded, setLoaded] = useState(false)
  const [failed, setFailed] = useState(false)
  const [dataUrl, setDataUrl] = useState<string | null>(null)

  useEffect(() => {
    setLoaded(false)
    setFailed(false)
    setDataUrl(null)
  }, [file.path])

  const ext = file.name.split('.').pop()?.toLowerCase() || ''
  const previewableSet = isWebMode ? WEB_PREVIEWABLE_EXTENSIONS : PREVIEWABLE_EXTENSIONS
  const canPreview = previewableSet.has(ext)

  useEffect(() => {
    if (!isWebMode || !canPreview || failed) return
    let cancelled = false
    window.electronAPI.readFilePreviewDataUrl(file.path, 64).then((url) => {
      if (!cancelled) setDataUrl(url)
    }).catch(() => {
      if (!cancelled) setFailed(true)
    })
    return () => { cancelled = true }
  }, [file.path, canPreview, failed])

  if (!canPreview || failed) {
    return getFileIcon(file)
  }

  const imgSrc = isWebMode ? dataUrl : getThumbnailUrl(file.path)

  return (
    <>
      <span
        className={cn(
          'absolute inset-0 flex items-center justify-center transition-opacity duration-200',
          loaded ? 'opacity-0' : 'opacity-100'
        )}
      >
        {getFileIcon(file)}
      </span>
      {imgSrc && (
        <img
          src={imgSrc}
          alt=""
          loading="lazy"
          onLoad={() => setLoaded(true)}
          onError={() => setFailed(true)}
          className={cn(
            'absolute inset-0 h-full w-full rounded-[2px] object-cover transition-opacity duration-200',
            loaded ? 'opacity-100' : 'opacity-0'
          )}
        />
      )}
    </>
  )
})

interface FileTreeItemProps {
  file: FileTreeEntry
  depth: number
  expandedPaths: Set<string>
  selectedPath?: string | null
  onToggleExpand: (path: string) => void
  onFileClick: (file: FileTreeEntry) => void
  onFileDoubleClick: (file: FileTreeEntry) => void
  onRevealInFileManager: (path: string) => void
  onCreateFolder?: (directory: FileTreeEntry) => void
  onRenameFolder?: (directory: FileTreeEntry) => void
  onDeleteFolder?: (directory: FileTreeEntry) => void
  onCopyPath?: (path: string) => void
}

function FileTreeItem({
  file,
  depth,
  expandedPaths,
  selectedPath,
  onToggleExpand,
  onFileClick,
  onFileDoubleClick,
  onRevealInFileManager,
  onCreateFolder,
  onRenameFolder,
  onDeleteFolder,
  onCopyPath,
}: FileTreeItemProps) {
  const { t } = useTranslation()
  const isDirectory = file.type === 'directory'
  const isExpanded = expandedPaths.has(file.path)
  const isSelected = selectedPath === file.path
  const hasChildren = isDirectory && file.children && file.children.length > 0

  const handleClick = () => {
    if (isDirectory && hasChildren) {
      onToggleExpand(file.path)
    } else {
      onFileClick(file)
    }
  }

  const handleDoubleClick = () => {
    onFileDoubleClick(file)
  }

  const handleChevronClick = (e: React.MouseEvent) => {
    e.stopPropagation()
    if (hasChildren) {
      onToggleExpand(file.path)
    }
  }

  const actionLabel = isDirectory ? (hasChildren ? 'expand' : 'open') : 'preview'

  const buttonElement = (
    <button
      onClick={handleClick}
      onDoubleClick={handleDoubleClick}
      className={cn(
        "group flex w-full min-w-0 overflow-hidden items-center gap-2 rounded-[6px] py-[5px] text-[13px] select-none outline-none text-left",
        "focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-ring",
        "hover:bg-sidebar-hover transition-colors px-2",
        isSelected && "bg-foreground/[0.07]"
      )}
      title={`${file.path}\n${file.type === 'file' ? formatFileSize(file.size) : 'Directory'}\n\nClick to ${actionLabel}`}
    >
      <span className="relative h-3.5 w-3.5 shrink-0 flex items-center justify-center">
        {hasChildren ? (
          <>
            <span className="absolute inset-0 flex items-center justify-center group-hover:opacity-0 transition-opacity duration-150">
              {getFileIcon(file, isExpanded)}
            </span>
            <span
              className="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity duration-150 cursor-pointer"
              onClick={handleChevronClick}
            >
              <ChevronRight
                className={cn(
                  "h-3.5 w-3.5 text-muted-foreground transition-transform duration-200",
                  isExpanded && "rotate-90"
                )}
              />
            </span>
          </>
        ) : (
          <FileThumbnail file={file} />
        )}
      </span>

      <span className="flex-1 min-w-0 truncate">{file.name}</span>
    </button>
  )

  const fileManagerName = getFileManagerName()

  return (
    <div className="group/section min-w-0">
      <ContextMenu>
        <ContextMenuTrigger asChild>
          {buttonElement}
        </ContextMenuTrigger>
        <StyledContextMenuContent>
          {file.type !== 'directory' && (
            <StyledContextMenuItem onSelect={() => onFileClick(file)}>
              <ExternalLink className="h-3.5 w-3.5" />
              {t("chat.openFile")}
            </StyledContextMenuItem>
          )}
          {file.type === 'directory' && onCreateFolder && (
            <StyledContextMenuItem onSelect={() => onCreateFolder(file)}>
              <FolderPlus className="h-3.5 w-3.5" />
              {t("common.newFolder")}
            </StyledContextMenuItem>
          )}
          {file.type === 'directory' && onRenameFolder && (
            <StyledContextMenuItem onSelect={() => onRenameFolder(file)}>
              <Pencil className="h-3.5 w-3.5" />
              {t("common.rename")}
            </StyledContextMenuItem>
          )}
          {onCopyPath && (
            <StyledContextMenuItem onSelect={() => onCopyPath(file.path)}>
              <Copy className="h-3.5 w-3.5" />
              {t("common.copyPath")}
            </StyledContextMenuItem>
          )}
          <StyledContextMenuItem
            onSelect={() => onRevealInFileManager(file.path)}
          >
            {isWebMode ? <Download className="h-3.5 w-3.5" /> : <FolderOpen className="h-3.5 w-3.5" />}
            {isWebMode ? t("common.download") : t("chat.showInFileManager", { fileManager: fileManagerName })}
          </StyledContextMenuItem>
          {file.type === 'directory' && onDeleteFolder && (
            <StyledContextMenuSeparator />
          )}
          {file.type === 'directory' && onDeleteFolder && (
            <StyledContextMenuItem
              onSelect={() => onDeleteFolder(file)}
              variant="destructive"
            >
              <Trash2 className="h-3.5 w-3.5" />
              {t("common.deleteFolder")}
            </StyledContextMenuItem>
          )}
        </StyledContextMenuContent>
      </ContextMenu>
      {hasChildren && (
        <AnimatePresence initial={false}>
          {isExpanded && (
            <motion.div
              initial={{ height: 0, opacity: 0, marginTop: 0, marginBottom: 0 }}
              animate={{ height: 'auto', opacity: 1, marginTop: 2, marginBottom: 8 }}
              exit={{ height: 0, opacity: 0, marginTop: 0, marginBottom: 0 }}
              transition={{ duration: 0.2, ease: 'easeInOut' }}
              className="overflow-hidden"
            >
              <div className="flex flex-col select-none min-w-0">
                <motion.nav
                  className="grid gap-0.5 pl-5 pr-0 relative"
                  variants={containerVariants}
                  initial="hidden"
                  animate="visible"
                  exit="exit"
                >
                  <div
                    className="absolute left-[13px] top-1 bottom-1 w-px bg-foreground/10"
                    aria-hidden="true"
                  />
                  {file.children!.map((child) => (
                    <motion.div key={child.path} variants={itemVariants} className="min-w-0">
                      <FileTreeItem
                        file={child}
                        depth={depth + 1}
                        expandedPaths={expandedPaths}
                        selectedPath={selectedPath}
                        onToggleExpand={onToggleExpand}
                        onFileClick={onFileClick}
                        onFileDoubleClick={onFileDoubleClick}
                        onRevealInFileManager={onRevealInFileManager}
                        onCreateFolder={onCreateFolder}
                        onRenameFolder={onRenameFolder}
                        onDeleteFolder={onDeleteFolder}
                        onCopyPath={onCopyPath}
                      />
                    </motion.div>
                  ))}
                </motion.nav>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      )}
    </div>
  )
}

export interface FileTreeProps {
  files: FileTreeEntry[]
  expandedPaths: Set<string>
  selectedPath?: string | null
  onToggleExpand: (path: string) => void
  onFileClick: (file: FileTreeEntry) => void
  onFileDoubleClick: (file: FileTreeEntry) => void
  onRevealInFileManager: (path: string) => void
  onCreateFolder?: (directory: FileTreeEntry) => void
  onRenameFolder?: (directory: FileTreeEntry) => void
  onDeleteFolder?: (directory: FileTreeEntry) => void
  onCopyPath?: (path: string) => void
  withRootGuides?: boolean
}

export function FileTree({
  files,
  expandedPaths,
  selectedPath,
  onToggleExpand,
  onFileClick,
  onFileDoubleClick,
  onRevealInFileManager,
  onCreateFolder,
  onRenameFolder,
  onDeleteFolder,
  onCopyPath,
  withRootGuides = false,
}: FileTreeProps) {
  return (
    <nav className={cn("grid gap-0.5 relative", withRootGuides ? "pl-7 pr-2" : "px-2")}>
      {withRootGuides && (
        <div
          className="absolute left-[21px] top-1 bottom-1 w-px bg-foreground/10"
          aria-hidden="true"
        />
      )}
      {files.map((file) => (
        <FileTreeItem
          key={file.path}
          file={file}
          depth={0}
          expandedPaths={expandedPaths}
          selectedPath={selectedPath}
          onToggleExpand={onToggleExpand}
          onFileClick={onFileClick}
          onFileDoubleClick={onFileDoubleClick}
          onRevealInFileManager={onRevealInFileManager}
          onCreateFolder={onCreateFolder}
          onRenameFolder={onRenameFolder}
          onDeleteFolder={onDeleteFolder}
          onCopyPath={onCopyPath}
        />
      ))}
    </nav>
  )
}
