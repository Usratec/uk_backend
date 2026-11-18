# Sourced by deploy_aws_dev.sh. Prepare every revision before changing services.
prepare_dev_workers() {
    local services service_arn worker revision new_worker_revision
    services=$(aws ecs list-services --region "$region" --cluster "$cluster"         --query 'serviceArns[]' --output json) || return
    jq -e 'type == "array" and all(.[]; type == "string")' <<< "$services" >/dev/null || return

    : > "$deploy_tmp_dir/workers.tsv"
    while IFS= read -r service_arn; do
        worker=${service_arn##*/}
        [[ "$worker" != "$service" ]] || continue

        revision=$(aws ecs describe-services --region "$region" --cluster "$cluster"             --services "$service_arn" --query 'services[0].taskDefinition' --output text) || return
        aws ecs describe-task-definition --region "$region" --task-definition "$revision"             --include TAGS --output json > "$deploy_tmp_dir/worker-current.json" || return

        # The cluster is shared with Stage and other applications. Match the full
        # Dev image repository and leave every other container image in place.
        jq --arg repository "$repository" --arg image "$image" '
            .taskDefinition + (if ((.tags // []) | length) > 0 then {tags: .tags} else {} end)
            | def backend_image: (.image | split("@")[0] | split(":")[0]) == $repository;
            select(any(.containerDefinitions[]; backend_image))
            | .containerDefinitions |= map(if backend_image then .image = $image else . end)
            | del(.taskDefinitionArn, .revision, .status, .requiresAttributes, .compatibilities,
                  .registeredAt, .registeredBy, .deregisteredAt)
        ' "$deploy_tmp_dir/worker-current.json" > "$deploy_tmp_dir/worker-updated.json" || return
        [[ -s "$deploy_tmp_dir/worker-updated.json" ]] || continue

        new_worker_revision=$(aws ecs register-task-definition --region "$region"             --cli-input-json "file://$deploy_tmp_dir/worker-updated.json"             --query 'taskDefinition.taskDefinitionArn' --output text) || return
        printf '%s\t%s\t%s\n' "$worker" "$revision" "$new_worker_revision" >> "$deploy_tmp_dir/workers.tsv"
    done < <(jq -r '.[]' <<< "$services")

    if [[ ! -s "$deploy_tmp_dir/workers.tsv" ]]; then
        printf 'No separate ECS services use the Dev backend image repository. Check the worker task definitions.\n' >&2
        return 1
    fi
}
