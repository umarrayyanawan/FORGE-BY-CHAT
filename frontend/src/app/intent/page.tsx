"use client";

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { intentApi } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import type { ProjectIntent } from "@/types";

export default function IntentPage() {
  const [description, setDescription] = useState("");
  const [intent, setIntent] = useState<ProjectIntent | null>(null);

  const parseMutation = useMutation({
    mutationFn: (desc: string) => intentApi.parse(desc),
    onSuccess: (data) => setIntent(data),
  });

  return (
    <div className="mx-auto max-w-2xl px-6 py-10">
      <h1 className="mb-6 text-2xl font-bold text-forge-text">Intent Parser</h1>

      <Card className="mb-6">
        <CardHeader>
          <CardTitle>Describe your project</CardTitle>
        </CardHeader>
        <CardContent>
          <textarea
            className="mb-4 w-full resize-none rounded-lg border border-forge-border bg-forge-bg px-4 py-3 text-sm text-forge-text placeholder-forge-muted focus:border-forge-accent focus:outline-none"
            rows={4}
            placeholder="What do you want to build?"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
          <Button
            onClick={() => parseMutation.mutate(description)}
            disabled={!description.trim() || parseMutation.isPending}
          >
            {parseMutation.isPending ? "Parsing..." : "Parse Intent"}
          </Button>
        </CardContent>
      </Card>

      {intent && (
        <Card>
          <CardHeader>
            <CardTitle>{intent.project_name}</CardTitle>
            <Badge variant={intent.confidence_score > 0.7 ? "success" : "warning"}>
              {Math.round(intent.confidence_score * 100)}% confidence
            </Badge>
          </CardHeader>
          <CardContent>
            <p className="mb-3 text-forge-text">{intent.description}</p>
            <div className="mb-3">
              <p className="mb-1 text-xs font-semibold uppercase text-forge-muted">Features</p>
              <div className="flex flex-wrap gap-2">
                {intent.features.map((f) => (
                  <Badge key={f}>{f}</Badge>
                ))}
              </div>
            </div>
            <Button
              variant="ghost"
              size="sm"
              onClick={() =>
                (window.location.href = `/pipeline?project_id=${intent.intent_id}`)
              }
            >
              Continue to Pipeline
            </Button>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
