package botauth

import (
	"strings"
	"unicode"

	"github.com/bwmarrin/discordgo"
)

func IsAllowlisted(interaction *discordgo.InteractionCreate, allowedUserIDs []string) bool {
	userID := normalizeUserID(interactionUserID(interaction))
	if userID == "" || len(allowedUserIDs) == 0 {
		return false
	}
	for _, allowedUserID := range allowedUserIDs {
		if normalizeUserID(allowedUserID) == userID {
			return true
		}
	}
	return false
}

func ParseUserIDs(raw string) []string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil
	}
	tokens := strings.FieldsFunc(raw, func(r rune) bool {
		return unicode.IsSpace(r) || r == ',' || r == ';'
	})
	seen := make(map[string]struct{}, len(tokens))
	ids := make([]string, 0, len(tokens))
	for _, token := range tokens {
		id := normalizeUserID(token)
		if id == "" {
			continue
		}
		if _, ok := seen[id]; ok {
			continue
		}
		seen[id] = struct{}{}
		ids = append(ids, id)
	}
	return ids
}

func interactionUserID(interaction *discordgo.InteractionCreate) string {
	if interaction == nil {
		return ""
	}
	if interaction.Member != nil && interaction.Member.User != nil {
		return strings.TrimSpace(interaction.Member.User.ID)
	}
	if interaction.User != nil {
		return strings.TrimSpace(interaction.User.ID)
	}
	return ""
}

func normalizeUserID(value string) string {
	value = strings.TrimSpace(value)
	value = strings.TrimPrefix(value, "<@!")
	value = strings.TrimPrefix(value, "<@")
	value = strings.TrimPrefix(value, "<")
	value = strings.TrimSuffix(value, ">")
	return strings.TrimSpace(value)
}
