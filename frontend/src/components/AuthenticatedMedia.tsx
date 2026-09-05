import { useEffect, useState, type ImgHTMLAttributes } from "react";
import { downloadMedia, fetchMediaBlob, mediaUrl } from "../api/client";

function useAuthenticatedMediaUrl(source: string): string {
  const [resolved, setResolved] = useState(() =>
    source.startsWith("/api/") ? "" : mediaUrl(source),
  );

  useEffect(() => {
    if (!source.startsWith("/api/")) {
      setResolved(mediaUrl(source));
      return;
    }
    let active = true;
    let objectUrl = "";
    fetchMediaBlob(source)
      .then(({ blob }) => {
        if (!active) return;
        objectUrl = URL.createObjectURL(blob);
        setResolved(objectUrl);
      })
      .catch(() => {
        if (active) setResolved("");
      });
    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [source]);

  return resolved;
}

/** An image whose bytes are fetched with the current API Bearer token. */
export function AuthenticatedImage({ src = "", ...props }: ImgHTMLAttributes<HTMLImageElement>) {
  const resolved = useAuthenticatedMediaUrl(src);
  return <img {...props} src={resolved || undefined} />;
}

/** An authenticated image that can be opened at full size without exposing the token. */
export function AuthenticatedImageLink({
  src,
  alt,
  imageClassName,
  className,
  title,
}: {
  src: string;
  alt: string;
  imageClassName?: string;
  className?: string;
  title?: string;
}) {
  const resolved = useAuthenticatedMediaUrl(src);
  return (
    <a
      href={resolved || undefined}
      target="_blank"
      rel="noreferrer"
      className={className}
      title={title}
      aria-disabled={!resolved}
    >
      <img src={resolved || undefined} alt={alt} className={imageClassName} />
    </a>
  );
}

/** A link that downloads protected API media on demand with Bearer authentication. */
export function AuthenticatedDownloadLink({
  href,
  filename,
  children,
  className,
  title,
}: {
  href: string;
  filename?: string;
  children: React.ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <a
      href={mediaUrl(href)}
      className={className}
      title={title}
      onClick={(event) => {
        if (!href.startsWith("/api/")) return;
        event.preventDefault();
        void downloadMedia(href, filename);
      }}
    >
      {children}
    </a>
  );
}
